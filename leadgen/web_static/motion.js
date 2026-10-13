// Site motion: the landing hero intro, scroll reveal, smooth <details> and menu exits.
// Progressive enhancement: without JS, Web Animations/IntersectionObserver or with reduced
// motion the page renders as is. nav.js calls SiteMotion.init(root) after swapping content.
// Loaded without defer from <head>: the hero intro has to be armed before the first paint.
(function(){
  'use strict';
  var root=document.documentElement;
  var reduce=window.matchMedia?window.matchMedia('(prefers-reduced-motion: reduce)'):{matches:false};
  // Curves and durations come from the CSS tokens so JS and CSS motion stay identical.
  function token(name,fallback){
    var v=getComputedStyle(root).getPropertyValue(name).trim();
    return v||fallback;
  }
  function ms(name,fallback){
    var v=parseFloat(token(name,''));
    return isNaN(v)?fallback:v;
  }
  function ready(fn){
    if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',fn);
    else fn();
  }

  // ---- Hero intro: plays once per browser session, on the landing page only.
  var INTRO='sg-intro-played',introSeen=false;
  function introPlayed(){
    if(introSeen)return true;
    try{return sessionStorage.getItem(INTRO)==='1'}catch(err){return false}
  }
  function armIntro(){
    if(reduce.matches||introPlayed())return;
    introSeen=true;
    try{sessionStorage.setItem(INTRO,'1')}catch(err){}
    root.classList.add('intro');
  }
  if(location.pathname==='/')armIntro();

  function watchIntro(scope){
    if(!root.classList.contains('intro'))return;
    var hero=scope.querySelector('.hero'),mock=hero&&hero.querySelector('.mock');
    if(!mock){root.classList.remove('intro');return}
    var io=null;
    function finish(){
      root.classList.remove('intro');
      if(io)io.disconnect();
      hero.removeEventListener('animationend',settled);
      hero.removeEventListener('focusin',focused);
    }
    // When the last intro animation ends the class goes away; every animation ends on the resting style.
    function settled(){
      if(!hero.getAnimations)return;
      var running=hero.getAnimations({subtree:true}).some(function(a){
        return a.animationName&&a.animationName.indexOf('intro-')===0&&a.playState!=='finished';
      });
      if(!running)finish();
    }
    // Keyboard focus must never land on a control that is still transparent.
    function focused(e){
      if(e.target!==hero.querySelector('h1'))finish();
    }
    hero.addEventListener('animationend',settled);
    hero.addEventListener('focusin',focused);
    // Single column: the mock sits below the fold, so its story starts when it scrolls into view.
    var vh=window.innerHeight||root.clientHeight;
    if(window.IntersectionObserver&&mock.getBoundingClientRect().top>vh*.8){
      mock.classList.add('is-waiting');
      io=new IntersectionObserver(function(entries){
        if(!entries.some(function(e){return e.isIntersecting}))return;
        io.disconnect();io=null;
        // Skip the opening pause the side-by-side layout needs: the story starts as soon as it is seen.
        if(mock.getAnimations)mock.getAnimations({subtree:true}).forEach(function(a){
          if(a.animationName&&a.animationName.indexOf('intro-')===0&&a.currentTime<220)a.currentTime=220;
        });
        mock.classList.remove('is-waiting');
      },{rootMargin:'0px 0px -15% 0px'});
      io.observe(mock);
    }
  }

  // ---- Scroll reveal (public pages): only blocks that start below the fold get the hidden state,
  // so nothing already on screen flickers and a page without JS shows everything. The cabinet is a
  // work tool with dense data and gets no scroll choreography.
  var GROUPS='.pains,.features,.steps,.audience,.plans';
  var TARGETS='.section-head,.faq,.pricing-note,.cta .box,.pains>*,.features>*,.steps>*,.audience>*,.plans>*';
  var STEP=70,MAX_DELAY=280,FALLBACK=1400;
  var observer=null;

  function settle(el){
    el.classList.remove('reveal','is-revealed');
    el.style.removeProperty('--reveal-delay');
  }
  function show(batch){
    // Siblings that enter together rise one after another; the total delay stays short.
    var seen=[];
    batch.forEach(function(el){
      var n=0;
      for(var i=0;i<seen.length;i++)if(seen[i].parentNode===el.parentNode)n++;
      seen.push(el);
      el.style.setProperty('--reveal-delay',Math.min(n*STEP,MAX_DELAY)+'ms');
      el.classList.add('is-revealed');
      var done=function(e){
        if(e&&(e.target!==el||e.propertyName!=='transform'))return;
        el.removeEventListener('transitionend',done);
        settle(el);
      };
      el.addEventListener('transitionend',done);
      setTimeout(done,FALLBACK+MAX_DELAY);
    });
  }
  function reveal(scope){
    if(observer){observer.disconnect();observer=null}
    if(reduce.matches||!window.IntersectionObserver||document.querySelector('.app'))return;
    var vh=window.innerHeight||root.clientHeight;
    var items=[];
    Array.prototype.forEach.call(scope.querySelectorAll(TARGETS),function(el){
      if(el.matches(GROUPS)||el.closest('.hero'))return;
      if(el.parentElement&&el.parentElement.closest('.reveal'))return;
      if(el.getBoundingClientRect().top<vh)return;
      el.classList.add('reveal');
      items.push(el);
    });
    if(!items.length)return;
    observer=new IntersectionObserver(function(entries,obs){
      var batch=[];
      entries.forEach(function(e){
        if(!e.isIntersecting)return;
        obs.unobserve(e.target);
        batch.push(e.target);
      });
      if(batch.length)show(batch);
    },{rootMargin:'100000px 0px -8% 0px',threshold:0}); // blocks skipped past (jump to an anchor) count as seen
    items.forEach(function(el){observer.observe(el)});
  }

  window.SiteMotion={init:function(scope){
    scope=scope||document;
    // A swapped page never inherits a running intro; the landing gets one if this session has not seen it.
    root.classList.remove('intro');
    if(scope.querySelector('.hero .mock'))armIntro();
    watchIntro(scope);
    reveal(scope);
  }};
  ready(function(){watchIntro(document);reveal(document)});

  // ---- Disclosures and menus need Web Animations; without them native <details> stays.
  if(!root.animate)return;
  var SELECTOR='.faq details,details.fold,details.nav-more';
  var MENUS='.menu,.mobile-top .bar>details';
  root.classList.add('js-details');

  function closedHeight(el,summary){
    var cs=getComputedStyle(el);
    return summary.offsetHeight+parseFloat(cs.paddingTop)+parseFloat(cs.paddingBottom)+
      parseFloat(cs.borderTopWidth)+parseFloat(cs.borderBottomWidth);
  }
  function content(el){
    return Array.prototype.filter.call(el.children,function(c){return c.tagName!=='SUMMARY'});
  }
  function cleanup(el){
    el.style.height='';el.style.overflow='';
    el.classList.remove('is-closing','is-animating');
    el._fold=null;
  }

  // Height, content and the summary icon share one duration and curve; a second click mid-way
  // reverses from wherever the animation is.
  function toggle(el,summary){
    var state=el._fold;
    var opening=state?!state.opening:!el.open;
    var start=el.getBoundingClientRect().height;
    var items=content(el);
    var fromOpacity=items.map(function(c){return state?parseFloat(getComputedStyle(c).opacity):(opening?0:1)});
    if(state){state.anims.forEach(function(a){a.cancel()})}
    el.classList.add('is-animating');
    el.classList.toggle('is-closing',!opening);
    el.style.overflow='hidden';
    el.style.height='';
    if(opening)el.open=true;
    var end=opening?el.offsetHeight:closedHeight(el,summary);
    var time=ms('--dur-fold',340),ease=token('--ease-standard','cubic-bezier(.32,.72,0,1)');
    var anims=[el.animate({height:[start+'px',end+'px']},{duration:time,easing:ease})];
    items.forEach(function(c,i){
      var from=fromOpacity[i],to=opening?1:0;
      // Opening: text follows the growing height a beat later. Closing: it fades out front-loaded, so it is
      // gone before the shrinking fold would cut through it.
      anims.push(c.animate({
        opacity:[from,to],
        transform:['translateY('+(-6*(1-from))+'px)','translateY('+(-6*(1-to))+'px)']
      },opening?{duration:time,delay:state?0:time*.12,easing:token('--ease-out-expo',ease),fill:'both'}
               :{duration:time*.5,easing:ease,fill:'both'}));
    });
    state={opening:opening,anims:anims};
    el._fold=state;
    anims[0].onfinish=function(){
      if(el._fold!==state)return;
      if(!opening)el.open=false;
      anims.forEach(function(a){a.cancel()});
      cleanup(el);
    };
  }

  // Menus open with a CSS entrance; closing plays a short exit before the panel is removed.
  function closeMenu(el){
    var panel=el.querySelector('.site-nav,.drawer');
    if(!panel){el.open=false;return}
    el.classList.add('is-closing');
    var timer=null;
    var done=function(e){
      if(e&&e.target!==panel)return;
      panel.removeEventListener('animationend',done);
      clearTimeout(timer);
      if(!el.classList.contains('is-closing'))return;
      el.classList.remove('is-closing');
      el.open=false;
    };
    panel.addEventListener('animationend',done);
    timer=setTimeout(done,ms('--dur-fast',150)+120);
  }

  document.addEventListener('click',function(e){
    if(reduce.matches||document.hidden||e.defaultPrevented||e.button)return;
    var summary=e.target.closest&&e.target.closest('summary');
    if(!summary)return;
    var el=summary.parentElement;
    if(!el||el.tagName!=='DETAILS'||el.firstElementChild!==summary)return;
    if(el.matches(MENUS)){
      if(el.classList.contains('is-closing')){e.preventDefault();el.classList.remove('is-closing');return}
      if(el.open){e.preventDefault();closeMenu(el)}
      return;
    }
    if(!el.matches(SELECTOR))return;
    e.preventDefault();
    toggle(el,summary);
  });
})();
