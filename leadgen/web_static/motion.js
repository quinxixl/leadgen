// Site motion: smooth <details> open/close and scroll reveal of content blocks.
// Progressive enhancement: without JS, Web Animations/IntersectionObserver or with reduced
// motion the page renders as is. nav.js calls SiteMotion.init(root) after swapping content.
(function(){
  'use strict';
  var root=document.documentElement;
  var reduce=window.matchMedia?window.matchMedia('(prefers-reduced-motion: reduce)'):{matches:false};

  // Scroll reveal: only blocks that start below the fold get the hidden state, so nothing
  // already on screen flickers and a page without JS shows everything.
  var GROUPS='.pains,.features,.steps,.audience,.plans,.grid,.pipeline-board';
  var BLOCKS='.section-head,.faq,.pricing-note,.cta .box,'+
    '.workspace>section,.workspace>article,.workspace>.panel,.workspace>details.panel';
  var TARGETS=BLOCKS+',.pains>*,.features>*,.steps>*,.audience>*,.plans>*,.grid>*,.pipeline-board>*';
  var STEP=60,MAX_DELAY=200,FALLBACK=1200;
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
        if(e&&e.target!==el)return;
        el.removeEventListener('transitionend',done);
        settle(el);
      };
      el.addEventListener('transitionend',done);
      setTimeout(done,FALLBACK+MAX_DELAY);
    });
  }
  function reveal(scope){
    if(observer){observer.disconnect();observer=null}
    if(reduce.matches||!window.IntersectionObserver)return;
    var vh=window.innerHeight||root.clientHeight;
    var items=[];
    Array.prototype.forEach.call((scope||document).querySelectorAll(TARGETS),function(el){
      if(el.matches(GROUPS)||el.closest('.lead-list')||el.matches('.lead-list'))return;
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

  window.SiteMotion={init:function(scope){reveal(scope)}};
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',function(){reveal(document)});
  else reveal(document);

  // Disclosures need Web Animations; without them native <details> stays.
  if(!root.animate)return;
  var SELECTOR='.faq details,details.fold,details.nav-more';
  var EASE='cubic-bezier(.2,.8,.2,1)';
  root.classList.add('js-details');

  function duration(){
    var v=parseFloat(getComputedStyle(root).getPropertyValue('--dur-fold'));
    return isNaN(v)?280:v;
  }
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
    var time=duration();
    var anims=[el.animate({height:[start+'px',end+'px']},{duration:time,easing:EASE})];
    items.forEach(function(c,i){
      var from=fromOpacity[i],to=opening?1:0;
      anims.push(c.animate({
        opacity:[from,to],
        transform:['translateY('+(-6*(1-from))+'px)','translateY('+(-6*(1-to))+'px)']
      },{duration:opening?time:time*.7,easing:EASE,fill:'forwards'}));
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

  document.addEventListener('click',function(e){
    if(reduce.matches||document.hidden||e.defaultPrevented||e.button)return;
    var summary=e.target.closest&&e.target.closest('summary');
    if(!summary)return;
    var el=summary.parentElement;
    if(!el||el.tagName!=='DETAILS'||el.firstElementChild!==summary||!el.matches(SELECTOR))return;
    e.preventDefault();
    toggle(el,summary);
  });
})();
