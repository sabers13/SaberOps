"use strict";
(function () {
  var app = document.getElementById('app');
  if (!app || !app.classList.contains('chat-shell')) return;
  var header = document.getElementById('run-header');
  var runId = header && header.dataset.runId || null;
  var runActive = !!header && header.dataset.canCancel === 'true';
  var canStopReply = false, stopping = false, checking = false, checkedAt = 0;
  var primary = document.getElementById('chatStop');
  var primarySend = document.getElementById('chatSend');
  var secondaryForm = document.getElementById('runComposerStopForm');
  var secondarySend = document.querySelector('.run-composer .composer-bar > .send-button');
  function render() {
    var work = app.dataset.conversationMode === 'work';
    var stopRun = work && runActive && !!runId;
    if (primary) {
      primary.hidden = !(stopRun || canStopReply);
      primary.dataset.stopRun = stopRun ? runId : '';
      primary.setAttribute('aria-label', stopping ? 'Stopping run' : stopRun ? 'Stop run' : 'Stop reply');
      primary.title = stopRun ? 'Stop run' : 'Stop reply';
      primary.disabled = stopping;
      if (primarySend) primarySend.hidden = stopRun || canStopReply;
    }
    if (secondaryForm) {
      secondaryForm.hidden = !stopRun;
      var button = secondaryForm.querySelector('button');
      button.disabled = stopping || !stopRun;
      button.setAttribute('aria-label', stopping ? 'Stopping run' : 'Stop run');
      button.title = 'Stop run';
      if (secondarySend) secondarySend.hidden = stopRun;
    }
  }
  async function stopRun(event) {
    event.preventDefault();
    if (!runId || !runActive || stopping || app.dataset.conversationMode !== 'work') return;
    stopping = true; render();
    var wrap = (event.currentTarget.closest('.composer-wrap'));
    var status = wrap.querySelector('.composer-control-status');
    if (!status) {status = document.createElement('p');status.className = 'composer-control-status';status.setAttribute('role','status');wrap.appendChild(status);}
    status.textContent = 'Requesting stop…'; status.hidden = false;
    try {
      var response = await fetch('/runs/' + encodeURIComponent(runId) + '/cancel', {method:'POST',credentials:'same-origin'});
      if (!response.ok) throw new Error('Could not stop the run. Try again.');
      if (new URL(response.url).searchParams.get('notice') === 'cancel_not_applicable') throw new Error('This run can no longer be stopped. Refresh its status.');
      status.textContent = 'Stop requested. Waiting for the run to finish stopping.';
      checkedAt = 0; await checkRun();
    } catch (error) {status.textContent = error.message;}
    finally {stopping = false; render();}
  }
  if (primary) primary.addEventListener('click', function (event) {if (primary.dataset.stopRun) stopRun(event);});
  if (secondaryForm) secondaryForm.addEventListener('submit', function (event) {
    event.stopImmediatePropagation(); stopRun(event);
  }, true);
  async function checkRun() {
    if (!runId || checking || Date.now() - checkedAt < 3000) return;
    checking = true;
    var requested = runId;
    try {
      var response = await fetch('/runs/' + encodeURIComponent(requested) + '/actions', {credentials:'same-origin'});
      if (!response.ok) return;
      var state = await response.json();
      if (runId === requested) {runActive = !!state.can_cancel; checkedAt = Date.now();render();}
    } finally {checking = false;}
  }
  window.SaberOpsComposerControls = {replyState:function (value) {canStopReply = value;render();}};
  document.addEventListener('saberops:conversation-mode', render);
  document.addEventListener('saberops:run-action-state', function (event) {
    if (event.detail.run_id === runId) {runActive = !!event.detail.can_cancel; render();}
  });
  document.addEventListener('saberops:chat-refreshed', function (event) {
    if (header) return;
    var runs = event.detail.runs || [];
    var latest = runs.length ? runs[runs.length - 1].run_id : null;
    if (latest !== runId) {runId = latest;runActive = false;checkedAt = 0;render();}
    if (app.dataset.conversationMode === 'work') checkRun().catch(function () { /* Keep the last authoritative state. */ });
  });
  render();
})();
