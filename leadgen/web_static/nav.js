// No-reload navigation for the header, footer and cabinet menu links.
// The target page is fetched as ordinary HTML and its <body> replaces the current one. Anything
// that is not a plain same-layout page (other scripts, other layout, redirects, errors) falls
// back to a normal page load, so every link keeps working without this script.
(function(){
  'use strict';
  if(!window.fetch||!window.DOMParser||!window.history||!history.pushState||!document.body)return;
  var NAV='.site-header,.site-footer,.sidebar,.mobile-top';
  // Pages with their own scripts or a separate security context always load in full.
  var FULL=/^\/(login|logout|admin|tg|api|web_static)(\/|$)/;
  var reduce=window.matchMedia?window.matchMedia('(prefers-reduced-motion: reduce)'):{matches:false};
  var pending=null,current=key(location);

  function key(u){return u.pathname+u.search}
  function layout(doc){return doc.querySelector('.app')?'app':'site'}
  // Scripts and styles the page loads; a page that needs anything different gets a full load.
  function assets(doc){
    var out=[];
    Array.prototype.forEach.call(doc.querySelectorAll('script'),function(s){
      var type=(s.getAttribute('type')||'').toLowerCase();
      if(type==='application/ld+json')return;
      out.push('s:'+(s.getAttribute('src')||'inline:'+s.textContent.length));
    });
    Array.prototype.forEach.call(doc.querySelectorAll('link[rel~="stylesheet"]'),function(l){out.push('c:'+l.getAttribute('href'))});
    return out.sort().join('|');
  }
  var signature=assets(document),shape=layout(document);

  function eligible(a,e){
    if(e.defaultPrevented||e.button!==0||e.metaKey||e.ctrlKey||e.shiftKey||e.altKey)return null;
    if(!a.closest(NAV)||a.hasAttribute('download')||a.hasAttribute('data-no-pjax'))return null;
    var target=a.getAttribute('target');
    if(target&&target!=='_self')return null;
    var url;
    try{url=new URL(a.href,location.href)}catch(err){return null}
    if(url.origin!==location.origin||!/^https?:$/.test(url.protocol))return null;
    if(FULL.test(url.pathname)||/\.[a-z0-9]+$/i.test(url.pathname))return null;
    // The cabinet and the public site have different layouts and security headers.
    if((url.pathname==='/app'||url.pathname.indexOf('/app/')===0)!==(shape==='app'))return null;
    return url;
  }

  function closeMenus(){
    Array.prototype.forEach.call(document.querySelectorAll('.menu[open],.mobile-top .bar>details[open]'),function(d){d.open=false});
  }

  function saveScroll(){
    try{history.replaceState({pjax:true,y:window.scrollY},'',location.href)}catch(err){}
  }

  function full(url){location.href=url}

  function scrollTo(hash,y){
    var el=null;
    if(hash){try{el=document.getElementById(decodeURIComponent(hash.slice(1)))}catch(err){}}
    if(el){el.scrollIntoView({block:'start',behavior:'instant'});return el}
    window.scrollTo({top:y||0,left:0,behavior:'instant'});
    return null;
  }

  function focusMain(target){
    var el=target||document.querySelector('main h1')||document.querySelector('main');
    if(!el)return;
    if(!el.hasAttribute('tabindex')&&!/^(A|BUTTON|INPUT|SELECT|TEXTAREA|SUMMARY)$/.test(el.tagName))el.setAttribute('tabindex','-1');
    try{el.focus({preventScroll:true})}catch(err){el.focus()}
  }

  function swap(doc,url,y){
    document.title=doc.title;
    ['meta[name="description"]','link[rel="canonical"]','meta[property="og:title"]','meta[property="og:description"]','meta[property="og:url"]'].forEach(function(sel){
      var from=doc.head.querySelector(sel),to=document.head.querySelector(sel);
      if(from&&to)to.replaceWith(document.importNode(from,true));
    });
    var body=document.body,next=doc.body;
    Array.prototype.slice.call(body.attributes).forEach(function(a){body.removeAttribute(a.name)});
    Array.prototype.forEach.call(next.attributes,function(a){body.setAttribute(a.name,a.value)});
    body.replaceChildren.apply(body,Array.prototype.map.call(next.childNodes,function(n){return document.importNode(n,true)}));
    var target=scrollTo(url.hash,y);
    if(window.SiteMotion)window.SiteMotion.init(document);
    focusMain(target);
  }

  // Yandex Metrica (public pages only) counts the new page as a hit and fires its view goals.
  function track(url,referrer){
    var s=document.querySelector('script[src*="metrika.js"][data-id]');
    var id=s&&Number(s.getAttribute('data-id'));
    if(!id||typeof window.ym!=='function')return;
    window.ym(id,'hit',url.href,{title:document.title,referer:referrer});
    Array.prototype.forEach.call(document.querySelectorAll('[data-goal-view]'),function(el){window.ym(id,'reachGoal',el.getAttribute('data-goal-view'))});
  }

  function navigate(href,push,y){
    var url=new URL(href,location.href),referrer=location.href;
    if(pending)pending.abort();
    var ctrl=window.AbortController?new AbortController():null;
    pending=ctrl;
    document.documentElement.setAttribute('aria-busy','true');
    return fetch(url.href,{credentials:'same-origin',headers:{'Accept':'text/html','X-Requested-With':'pjax'},signal:ctrl&&ctrl.signal})
      .then(function(res){
        var type=res.headers.get('Content-Type')||'';
        if(!res.ok||res.redirected||type.indexOf('text/html')<0)throw new Error('full');
        return res.text();
      })
      .then(function(html){
        if(pending!==ctrl)return;
        var doc=new DOMParser().parseFromString(html,'text/html');
        if(layout(doc)!==shape||assets(doc)!==signature)throw new Error('full');
        if(push){
          saveScroll();
          if('scrollRestoration' in history)history.scrollRestoration='manual';
          history.pushState({pjax:true,y:0},'',url.href);
        }
        current=key(url);
        var run=function(){swap(doc,url,y);track(url,referrer)};
        if(!reduce.matches&&document.startViewTransition){
          document.startViewTransition(run);
        }else{
          run();
          var main=document.querySelector('main');
          if(!reduce.matches&&main&&main.animate)main.animate({opacity:[0,1]},{duration:200,easing:'cubic-bezier(.2,.8,.2,1)'});
        }
      })
      .catch(function(err){
        if(err&&err.name==='AbortError')return;
        full(url.href);
      })
      .then(function(){
        if(pending===ctrl){pending=null;document.documentElement.removeAttribute('aria-busy')}
      });
  }

  document.addEventListener('click',function(e){
    var a=e.target.closest&&e.target.closest('a[href]');
    if(!a)return;
    var url=eligible(a,e);
    if(!url)return;
    // Same page, different anchor: the browser scrolls smoothly on its own.
    if(key(url)===key(location)&&url.hash){closeMenus();return}
    e.preventDefault();
    closeMenus();
    navigate(url.href,true,0);
  });

  window.addEventListener('popstate',function(e){
    // Hash-only history entries stay with the browser.
    if(key(location)===current)return;
    navigate(location.href,false,e.state&&e.state.y||0);
  });

  // After a reload of a page reached without reloading, return to where the reader was.
  var initial=history.state;
  if(initial&&initial.pjax&&initial.y&&history.scrollRestoration==='manual'&&!location.hash)window.scrollTo(0,initial.y);
  saveScroll();
  var timer=null;
  window.addEventListener('scroll',function(){
    clearTimeout(timer);
    timer=setTimeout(saveScroll,150);
  },{passive:true});
})();
