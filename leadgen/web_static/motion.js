// Smooth open/close for site <details> (FAQ, .fold, sidebar «Ещё»).
// Progressive enhancement: without JS, Web Animations or with reduced motion
// the native <details> behaviour is left untouched.
(function(){
  'use strict';
  var root=document.documentElement;
  if(!root.animate)return;
  var reduce=window.matchMedia?window.matchMedia('(prefers-reduced-motion: reduce)'):{matches:false};
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
