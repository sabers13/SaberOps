"use strict";
(function () {
  function attachmentView(item) {
    var link = document.createElement("a"); link.className = "chat-attachment";
    link.href = item.url + "?download=true"; link.title = "Download " + item.name;
    if (item.mime.startsWith("image/")) {
      var image = document.createElement("img"); image.src = item.url; image.alt = item.name;
      image.loading = "lazy"; link.appendChild(image);
    }
    var label = document.createElement("span"); label.textContent = item.name; link.appendChild(label);
    return link;
  }
  window.SaberOpsAttachmentView = attachmentView;
  document.querySelectorAll('[data-chat-form], #startRunForm, #runConversationForm').forEach(function (form) {
    var wrap = form.closest('.composer-wrap'), input = form.querySelector('textarea');
    if (!wrap || !input) return;
    var selected = [], uploading = 0, key = 'saberops-attachment-draft:' + form.getAttribute('action');
    var required = input.required;
    var field = document.createElement('input'); field.type = 'hidden'; field.name = 'attachments';
    form.appendChild(field);
    var row = document.createElement('div'); row.className = 'chat-attachment-list'; row.hidden = true;
    input.before(row);
    var status = document.createElement('p'); status.className = 'chat-attachment-status'; status.setAttribute('role','status'); status.hidden = true;
    row.after(status);
    var files = document.createElement('input'); files.type = 'file'; files.multiple = true; files.hidden = true;
    files.accept = 'image/png,image/jpeg,image/webp,image/gif,.pdf,.docx,.txt,.md,.json,.csv,.tsv,.py,.js,.ts,.tsx,.jsx,.html,.css,.yaml,.yml,.toml,.xml,.log,.sql,.sh,.c,.cpp,.h,.rs,.go,.java,.ini';
    files.setAttribute('aria-label','Choose files or pictures'); form.appendChild(files);
    var attach = document.createElement('button'); attach.type = 'button'; attach.className = 'icon-button chat-attach-button';
    attach.setAttribute('aria-label','Attach files or pictures'); attach.title = 'Attach files or pictures'; attach.textContent = '+';
    wrap.querySelector('.composer-bar').prepend(attach);
    attach.addEventListener('click', function () { if (form.getAttribute('aria-busy') !== 'true') files.click(); });
    function announce(text) { status.textContent = text; status.hidden = !text; }
    function render() {
      row.replaceChildren(); row.hidden = !selected.length;
      field.value = selected.map(function (item) { return item.id; }).join(',');
      input.required = !selected.length && required;
      selected.forEach(function (item) {
        var chip = document.createElement('div'); chip.className = 'chat-attachment-chip'; chip.appendChild(attachmentView(item));
        var remove = document.createElement('button'); remove.type = 'button'; remove.className = 'icon-button'; remove.textContent = '×';
        remove.setAttribute('aria-label', 'Remove ' + item.name); remove.title = 'Remove attachment';
        remove.addEventListener('click', async function () {
          if (form.getAttribute('aria-busy') === 'true') return;
          try {
            var response = await fetch(item.url,{method:'DELETE',credentials:'same-origin'});
            if (!response.ok) throw new Error('This file is already saved with a message.');
            selected = selected.filter(function (value) {return value.id !== item.id;}); render();
          } catch (error) {announce(error.message);}
        });
        chip.appendChild(remove); row.appendChild(chip);
      });
      try { localStorage.setItem(key, JSON.stringify(selected)); } catch (_) { /* optional */ }
    }
    async function upload(list) {
      if (form.getAttribute('aria-busy') === 'true') return;
      if (selected.length + uploading + list.length > 5) {announce('Attach up to five files per message.');return;}
      uploading += list.length;
      for (var file of list) {
        announce('Uploading ' + file.name + '…');
        try {
          var data = new FormData();data.append('file',file);
          var response = await fetch('/chat/attachments',{method:'POST',body:data,credentials:'same-origin'});
          var result = await response.json();
          if (!response.ok) throw new Error(result.error || 'Upload failed. Try again.');
          selected.push(result); render(); announce('');
        } catch (error) {announce(error.message);}
        finally {uploading -= 1;}
      }
      files.value = '';
    }
    files.addEventListener('change', function () {upload(Array.from(files.files));});
    wrap.addEventListener('dragover', function (event) {if (Array.from(event.dataTransfer.types).includes('Files')) {event.preventDefault();wrap.classList.add('attachment-drag');}});
    wrap.addEventListener('dragleave', function () {wrap.classList.remove('attachment-drag');});
    wrap.addEventListener('drop', function (event) {if (event.dataTransfer.files.length) {event.preventDefault();wrap.classList.remove('attachment-drag');upload(Array.from(event.dataTransfer.files));}});
    input.addEventListener('paste', function (event) {var pasted = Array.from(event.clipboardData.files);if (pasted.length) {event.preventDefault();upload(pasted);}});
    form.addEventListener('submit', async function (event) {
      if (uploading) {event.preventDefault();event.stopImmediatePropagation();announce('Wait for the files to finish uploading.');return;}
      if (form.hasAttribute('data-chat-form') || !selected.length) return;
      event.preventDefault(); event.stopImmediatePropagation();
      if (form.getAttribute('aria-busy') === 'true') return;
      form.setAttribute('aria-busy', 'true'); announce('Sending…');
      try {
        var response = await fetch(form.action, {method:'POST',body:new FormData(form),credentials:'same-origin'});
        if (!response.ok || !response.redirected) {
          var result = await response.json();
          throw new Error(result.error || 'Your message could not be sent.');
        }
        form.saberopsClearAttachments();
        var runHeader = document.getElementById('run-header');
        var draftKey = runHeader ? 'saberops-run-chat-draft:' + runHeader.dataset.runId : 'saberops-new-chat-draft';
        try {localStorage.removeItem(draftKey);} catch (_) { /* optional */ }
        location.assign(response.url);
      } catch (error) {announce(error.message);}
      finally {form.removeAttribute('aria-busy');}
    }, true);
    form.saberopsClearAttachments = function () {selected = []; render();announce('');};
    try {selected = JSON.parse(localStorage.getItem(key) || '[]').filter(function (item) {return item.id && item.name && item.url && item.mime;}).slice(0,5);} catch (_) { /* optional */ }
    render();
  });
})();
