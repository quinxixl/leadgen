// Forms with data-confirm ask before submitting. «{amount}» in the message becomes the value of the form's
// amount field, or data-full when it is empty (admin refunds). In the Telegram Mini App the question is
// shown with the native Telegram dialog.
(function(){
  'use strict';
  var sdk=window.Telegram&&window.Telegram.WebApp;
  var tg=sdk&&sdk.initData&&sdk.showConfirm?sdk:null;
  document.addEventListener('submit',function(e){
    var form=e.target;
    if(!form.matches||!form.matches('form[data-confirm]'))return;
    var input=form.elements.amount;
    var value=(input&&input.value.trim())||form.getAttribute('data-full')||'';
    var number=Number(value.replace(',','.').replace(/\s/g,''));
    if(value&&isFinite(number))value=number.toFixed(2);
    var message=form.getAttribute('data-confirm').replace('{amount}',value.replace('.',','));
    if(tg){
      e.preventDefault();
      try{
        // form.submit() does not fire the submit event again.
        tg.showConfirm(message,function(ok){if(ok)form.submit()});
        return;
      }catch(err){}
      if(window.confirm(message))form.submit();
      return;
    }
    if(!window.confirm(message))e.preventDefault();
  });
})();
