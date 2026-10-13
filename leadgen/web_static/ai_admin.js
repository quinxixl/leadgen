// /admin/ai: confirmations for state-changing buttons, "select all" in the journal, instructions length.
// Every form works without this script.
(function(){
  'use strict';
  document.addEventListener('click',function(e){
    var button=e.target.closest&&e.target.closest('button[data-confirm]');
    if(button&&!window.confirm(button.getAttribute('data-confirm')))e.preventDefault();
  });
  Array.prototype.forEach.call(document.querySelectorAll('input[data-select-all]'),function(toggle){
    var name=toggle.getAttribute('data-select-all');
    toggle.addEventListener('change',function(){
      var form=toggle.form||document;
      Array.prototype.forEach.call(form.querySelectorAll('input[type="checkbox"][name="'+name+'"]'),function(box){
        box.checked=toggle.checked;
      });
    });
  });
  Array.prototype.forEach.call(document.querySelectorAll('textarea[data-counter]'),function(area){
    var out=document.getElementById(area.getAttribute('data-counter'));
    if(!out)return;
    var max=area.getAttribute('maxlength');
    function update(){out.textContent=area.value.length+' / '+max+' символов'}
    area.addEventListener('input',update);update();
  });
})();
