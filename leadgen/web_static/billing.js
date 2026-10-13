// «Подписка»: online payment through YooKassa in the cabinet and the Telegram Mini App, and the payment
// return page. Progressive enhancement: without JS the checkout form posts and the server redirects to
// YooKassa; the return page shows the stored status. Loaded at the end of the page content (not deferred),
// so the form switches to its scripted layout before the first paint.
(function(){
  'use strict';
  var sdk=window.Telegram&&window.Telegram.WebApp;
  var tg=sdk&&sdk.initData?sdk:null;
  var reduce=window.matchMedia?window.matchMedia('(prefers-reduced-motion: reduce)'):{matches:false};
  var EMAIL=/^[^@\s]+@[^@\s]+\.[^@\s]+$/;
  var POLL_EVERY=2000,POLL_FOR=60000;
  var KINDS={initial:'Новая подписка',upgrade:'Переход на тариф выше',downgrade:'Смена тарифа',same:'Текущий тариф'};
  var OFFLINE='Нет соединения. Проверьте интернет и попробуйте ещё раз.';
  var dateFormat=null;
  try{dateFormat=new Intl.DateTimeFormat('ru-RU',{day:'2-digit',month:'2-digit',year:'numeric',timeZone:'Europe/Moscow'})}catch(err){}

  function rub(value){
    var n=Number(value);
    if(!isFinite(n))return '';
    var whole=Math.round(n*100)%100===0;
    return n.toLocaleString('ru-RU',{minimumFractionDigits:whole?0:2,maximumFractionDigits:2})+' ₽';
  }
  function day(iso){
    if(!iso)return '';
    var d=new Date(iso);
    if(isNaN(d))return '';
    if(dateFormat)return dateFormat.format(d);
    return ('0'+d.getDate()).slice(-2)+'.'+('0'+(d.getMonth()+1)).slice(-2)+'.'+d.getFullYear();
  }
  function cap(text){text=String(text||'');return text.charAt(0).toUpperCase()+text.slice(1)}
  function parse(res){
    return res.json().catch(function(){return null}).then(function(data){
      if(!data||typeof data!=='object')data={ok:false};
      if(!res.ok)data.ok=false;
      if(!data.ok&&!data.error){
        data.error=res.status===403?'Подпиской управляет владелец или администратор команды.'
          :res.status===400?'Сессия формы истекла. Обновите страницу и попробуйте ещё раз.'
          :'Что-то пошло не так. Попробуйте ещё раз через минуту.';
      }
      return data;
    });
  }
  function getJSON(url){return fetch(url,{credentials:'same-origin',headers:{Accept:'application/json'}}).then(parse)}
  function postJSON(url,data){
    return fetch(url,{method:'POST',credentials:'same-origin',headers:{Accept:'application/json'},body:new URLSearchParams(data)}).then(parse);
  }
  function haptic(kind){
    try{if(tg&&tg.HapticFeedback)tg.HapticFeedback.notificationOccurred(kind)}catch(err){}
  }
  function scrollIntoView(el,block){
    if(el&&el.scrollIntoView)el.scrollIntoView({behavior:reduce.matches?'auto':'smooth',block:block||'nearest'});
  }

  // Polls the return JSON every 2 s for up to 60 s; done(data) gets the final payment, or the last
  // pending answer (or null when offline) after the timeout.
  function poll(base,id,done){
    var started=Date.now(),timer=null,stopped=false,last=null;
    function again(){
      if(Date.now()-started>=POLL_FOR){done(last);return}
      timer=setTimeout(tick,POLL_EVERY);
    }
    function tick(){
      getJSON(base+'/return?payment='+encodeURIComponent(id)).then(function(data){
        if(stopped)return;
        if(data.ok){
          last=data;
          if(data.status!=='pending'){done(data);return}
        }
        again();
      },function(){if(!stopped)again()});
    }
    tick();
    return {stop:function(){stopped=true;clearTimeout(timer)}};
  }

  // A block with [data-state] children (one visible), [data-when] controls and [data-field] slots.
  function stateView(scope){
    function each(sel,fn){Array.prototype.forEach.call(scope.querySelectorAll(sel),fn)}
    return {
      current:null,
      show:function(name){
        this.current=name;
        each('[data-state]',function(el){el.hidden=el.getAttribute('data-state')!==name});
        each('[data-when]',function(el){el.hidden=(' '+el.getAttribute('data-when')+' ').indexOf(' '+name+' ')<0});
      },
      field:function(name){return scope.querySelector('[data-field="'+name+'"]')},
      fill:function(data){
        var set=function(name,text){var el=this.field(name);if(el)el.textContent=text}.bind(this);
        var ends=day(data.ends_at);
        set('plan_name',data.plan_name||'');
        set('ends',ends);
        var wrap=this.field('ends-wrap');if(wrap)wrap.hidden=!ends;
        var note=this.field('autorenew-note');if(note)note.hidden=!data.autorenew;
        set('reason',cap(data.reason||'платёж отклонён'));
      },
      result:function(data){
        if(!data||data.status==='pending'){this.show('pending');return}
        this.fill(data);
        var ok=data.status==='succeeded';
        this.show(ok?'succeeded':'canceled');
        haptic(ok?'success':'error');
      }
    };
  }

  // --- return page --------------------------------------------------------------------------------
  function returnPage(box){
    var view=stateView(box),base=box.getAttribute('data-base'),id=box.getAttribute('data-payment-id');
    var running=null;
    function run(){
      if(running)running.stop();
      view.show('checking');
      running=poll(base,id,function(data){running=null;view.result(data)});
    }
    box.addEventListener('click',function(e){
      if(!e.target.closest('[data-retry]'))return;
      e.preventDefault();
      run();
    });
    var status=box.getAttribute('data-status');
    if(status==='pending')run();
    else view.show(status==='succeeded'?'succeeded':'canceled');
  }

  // --- checkout -----------------------------------------------------------------------------------
  function checkout(form){
    var base=form.getAttribute('data-base');
    var grid=form.querySelector('[data-pay-plans]');
    var summary=form.querySelector('[data-summary]');
    var preview=form.querySelector('[data-preview]');
    var errorBox=form.querySelector('[data-error]');
    var submit=form.querySelector('[data-submit]');
    var wait=form.querySelector('[data-wait]');
    var email=form.elements.email,consent=form.elements.consent;
    var paid=grid.getAttribute('data-paid')==='1';
    var main=tg&&tg.MainButton;
    var state={plan:null,data:null,seq:0,busy:false,payment:null,url:null,waiting:false};
    form.noValidate=true;
    form.classList.add('is-js');

    function period(){var r=form.querySelector('input[name="period"]:checked');return r?r.value:'month'}
    function cards(){return Array.prototype.slice.call(grid.querySelectorAll('[data-plan]'))}
    function field(name){return summary.querySelector('[data-field="'+name+'"]')}
    function set(name,text){var el=field(name);if(el)el.textContent=text}
    function row(name,show){var el=summary.querySelector('[data-row="'+name+'"]');if(el)el.hidden=!show}
    function showError(message,target){
      errorBox.textContent=message||'';
      errorBox.hidden=!message;
      [email,consent].forEach(function(el){if(el)el.removeAttribute('aria-invalid')});
      if(target){target.setAttribute('aria-invalid','true');target.focus()}
    }

    function labels(){
      var p=period();
      cards().forEach(function(card){
        var button=card.querySelector('[data-pick]');
        var same=card.getAttribute('data-current-period')===p;
        var selected=card.getAttribute('data-plan')===state.plan;
        button.disabled=same&&!selected;
        button.setAttribute('aria-pressed',selected?'true':'false');
        button.textContent=selected?'Выбран':same?'Подключён':paid?'Перейти':'Оплатить';
      });
    }
    function submitText(data){
      if(!data)return 'Оплатить';
      if(data.kind==='downgrade')return 'Запланировать смену тарифа';
      if(data.kind==='same')return 'Тариф уже подключён';
      return 'Оплатить '+rub(data.amount);
    }
    function syncMain(){
      if(!main)return;
      var d=state.data;
      if(!state.plan||!d||d.kind==='same'||state.waiting){main.hide();return}
      main.setText(submitText(d));
      if(state.busy){main.showProgress(false);main.disable()}else{main.hideProgress();main.enable()}
      main.show();
    }
    function busy(on){
      state.busy=on;
      submit.disabled=on||!state.data||state.data.kind==='same';
      submit.classList.toggle('is-busy',on);
      submit.textContent=on?(state.data&&state.data.kind==='downgrade'?'Сохраняем…':'Переходим к оплате…'):submitText(state.data);
      syncMain();
    }

    function render(d){
      state.data=d;
      set('kind',KINDS[d.kind]||'Оформление');
      set('title','«'+d.plan_name+'» на '+(d.period==='year'?'год':'месяц'));
      set('price',rub(d.price));
      row('credit',Number(d.credit)>0);
      set('credit','− '+rub(d.credit));
      set('amount',rub(d.amount));
      var down=d.kind==='downgrade';
      row('starts',down);
      set('starts',day(d.starts_at));
      set('ends-label',down?'Новый период до':d.kind==='same'?'Оплачено до':'Тариф будет действовать до');
      set('ends',day(d.ends_at));
      var message=field('message');
      if(down)message.textContent='Тариф сменится '+day(d.starts_at)+', сейчас ничего не списываем.';
      else if(d.kind==='same')message.textContent=d.message||'Этот тариф уже подключён.';
      message.hidden=!(down||d.kind==='same');
      busy(false);
    }
    function load(){
      var seq=++state.seq;
      state.data=null;
      preview.classList.add('is-loading');
      preview.setAttribute('aria-busy','true');
      showError('');
      busy(false);
      getJSON(base+'/preview?plan='+encodeURIComponent(state.plan)+'&period='+encodeURIComponent(period())).then(function(d){
        if(seq!==state.seq)return;
        preview.classList.remove('is-loading');
        preview.removeAttribute('aria-busy');
        if(!d.ok){showError(d.error||'Не удалось рассчитать стоимость. Попробуйте ещё раз.');return}
        render(d);
      },function(){
        if(seq!==state.seq)return;
        preview.classList.remove('is-loading');
        preview.removeAttribute('aria-busy');
        showError(OFFLINE);
      });
    }
    function pick(card){
      if(!card||state.busy)return;
      var code=card.getAttribute('data-plan');
      if(card.getAttribute('data-current-period')===period()&&code!==state.plan)return;
      var first=!state.plan;
      if(state.waiting)leaveWaiting();
      state.plan=code;
      cards().forEach(function(c){c.classList.toggle('is-selected',c===card)});
      form.classList.add('has-plan');
      labels();
      load();
      // The summary sits under the cards: bring it into view (on phones it is a screen or two below).
      requestAnimationFrame(function(){scrollIntoView(summary,first?'start':'nearest')});
    }
    // Another plan chosen after opening the payment page: back to the summary (the open payment simply expires).
    function leaveWaiting(){
      state.waiting=false;
      if(running){running.stop();running=null}
      form.classList.remove('is-waiting');
      if(wait)wait.hidden=true;
      summary.hidden=false;
    }
    function reset(){
      leaveWaiting();
      state.plan=null;state.data=null;state.seq++;
      cards().forEach(function(c){c.classList.remove('is-selected')});
      form.classList.remove('has-plan');
      showError('');
      labels();
      syncMain();
      scrollIntoView(grid,'start');
    }

    grid.addEventListener('click',function(e){
      var card=e.target.closest('[data-plan]');
      if(!card)return;
      if(e.target.closest('a'))return;
      e.preventDefault();
      pick(card);
    });
    form.addEventListener('change',function(e){
      if(e.target.name!=='period')return;
      grid.setAttribute('data-period',period());
      grid.classList.add('is-live');
      labels();
      if(state.plan)load();
    });
    form.addEventListener('input',function(e){
      if(e.target===email||e.target===consent)showError('');
    });
    form.addEventListener('click',function(e){
      if(e.target.closest('[data-reset]')){e.preventDefault();reset()}
    });

    function validate(){
      var value=email.value.trim();
      email.value=value;
      if(!EMAIL.test(value)||value.length>254){showError('Укажите e-mail для чека, например name@example.ru.',email);return false}
      if(!consent.checked){showError('Подтвердите согласие с условиями автоматического продления.',consent);return false}
      return true;
    }
    function send(){
      if(state.busy||!state.data||state.data.kind==='same')return;
      if(!validate()){haptic('error');return}
      showError('');
      busy(true);
      postJSON(base+'/checkout',{csrf:form.elements.csrf.value,plan:state.plan,period:period(),
        email:email.value,consent:'1'}).then(function(d){
        if(!d.ok){busy(false);showError(d.error);haptic('error');return}
        if(d.status==='scheduled'){
          var message=field('message');
          message.textContent=d.message||'Смена тарифа запланирована.';
          message.hidden=false;
          haptic('success');
          setTimeout(function(){location.reload()},1400);
          return;
        }
        if(d.status==='redirect'&&d.confirmation_url){
          state.payment=d.payment_id;state.url=d.confirmation_url;
          if(tg&&wait)openInTelegram();
          else location.assign(d.confirmation_url);
          return;
        }
        busy(false);
        showError('Не удалось перейти к оплате. Попробуйте ещё раз.');
      },function(){busy(false);showError(OFFLINE)});
    }
    form.addEventListener('submit',function(e){
      // A plan button submitted the form before a plan was chosen in the script: pick it instead.
      if(!state.plan){
        e.preventDefault();
        var button=e.submitter&&e.submitter.closest('[data-plan]');
        if(button)pick(button);
        return;
      }
      e.preventDefault();
      send();
    });
    // Back from YooKassa via the browser history: the page may come from the back/forward cache.
    window.addEventListener('pageshow',function(e){if(e.persisted&&state.busy)busy(false)});

    // --- Telegram Mini App: the payment page opens in the browser, the user comes back and checks. ---
    var waitView=wait?stateView(wait):null,running=null;
    function check(){
      if(!state.payment||!waitView)return;
      if(running)running.stop();
      waitView.show('checking');
      running=poll(base,state.payment,function(data){running=null;waitView.result(data)});
    }
    function openInTelegram(){
      try{tg.openLink(state.url)}catch(err){location.assign(state.url);return}
      state.waiting=true;
      busy(false);
      summary.hidden=true;
      form.classList.add('is-waiting');
      wait.hidden=false;
      waitView.show('idle');
      syncMain();
      scrollIntoView(wait,'start');
    }
    if(wait){
      wait.addEventListener('click',function(e){
        if(e.target.closest('[data-check]'))check();
        else if(e.target.closest('[data-reopen]')&&state.url){
          try{tg.openLink(state.url)}catch(err){location.assign(state.url)}
          waitView.show('idle');
        }
        else if(e.target.closest('[data-done]'))location.reload();
        else if(e.target.closest('[data-again]'))reset();
      });
      // Coming back from the browser: check at once instead of waiting for a tap.
      var back=function(){if(state.waiting&&!document.hidden&&waitView.current==='idle')check()};
      document.addEventListener('visibilitychange',back);
      try{if(tg&&tg.onEvent)tg.onEvent('activated',back)}catch(err){}
    }
    if(main)main.onClick(function(){if(!state.waiting)send()});

    labels();
  }

  Array.prototype.forEach.call(document.querySelectorAll('form[data-checkout]'),checkout);
  Array.prototype.forEach.call(document.querySelectorAll('[data-payment-return]'),returnPage);
})();
