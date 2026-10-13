// Forms with data-confirm ask before submitting. «{amount}» in the message becomes the value of the form's
// amount field, or data-full when it is empty (admin refunds).
(function(){
  'use strict';
  document.addEventListener('submit',function(e){
    var form=e.target;
    if(!form.matches||!form.matches('form[data-confirm]'))return;
    var input=form.elements.amount;
    var value=(input&&input.value.trim())||form.getAttribute('data-full')||'';
    var number=Number(value.replace(',','.').replace(/\s/g,''));
    if(value&&isFinite(number))value=number.toFixed(2);
    var message=form.getAttribute('data-confirm').replace('{amount}',value.replace('.',','));
    if(!window.confirm(message))e.preventDefault();
  });
})();
