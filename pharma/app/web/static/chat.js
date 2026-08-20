/* Aria Pharma chat client. Vanilla JS, no libraries.
   All model-controlled text is rendered via textContent — never innerHTML. */
(function () {
  "use strict";

  var app = document.getElementById("chat-app");
  if (!app) return;

  var state = {
    conversationId: app.dataset.conversation || null,
    analysisId: null,
    after: 0,
    attachments: [],
    turn: null,       // current assistant turn container
    streamEl: null,   // streaming bubble inside the turn
    questionCard: null,
  };

  var threadInner = document.getElementById("thread-inner");
  var thread = document.getElementById("thread");
  var form = document.getElementById("composer-form");
  var questionBox = document.getElementById("question");
  var sendBtn = document.getElementById("send-btn");
  var chips = document.getElementById("chips");
  var fileInput = document.getElementById("file-input");
  var dropZone = document.getElementById("drop-zone");

  function scrollDown() { thread.scrollTop = thread.scrollHeight; }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  // ---- renderers ----
  function addUserBubble(text) {
    var msg = el("div", "msg user");
    msg.appendChild(el("span", "role", "you"));
    msg.appendChild(el("div", "body", text));
    threadInner.appendChild(msg);
    scrollDown();
  }

  function ensureTurn() {
    if (!state.turn) {
      state.turn = el("div", "msg assistant");
      state.turn.appendChild(el("span", "role", "analyst"));
      threadInner.appendChild(state.turn);
    }
    return state.turn;
  }

  function freezeText(text) {
    clearStream();
    ensureTurn().appendChild(el("div", "part", text));
    scrollDown();
  }

  function setStreaming(text) {
    var turn = ensureTurn();
    if (!state.streamEl) {
      state.streamEl = el("div", "stream");
      state.streamEl.appendChild(el("span", "stream-text", ""));
      state.streamEl.appendChild(el("span", "cursor", "▍"));
      turn.appendChild(state.streamEl);
    }
    state.streamEl.firstChild.textContent = text;
    turn.appendChild(state.streamEl); // keep beneath newest tool lines
    scrollDown();
  }

  function clearStream() {
    if (state.streamEl) { state.streamEl.remove(); state.streamEl = null; }
  }

  function addToolLine(text, kind) {
    var turn = ensureTurn();
    var prev = turn.querySelector(".tool-line .spin");
    if (prev) prev.remove();
    var line = el("div", "tool-line" + (kind === "error" ? " feed-error" : "") +
                          (kind === "lifecycle" ? " lifecycle" : ""));
    line.appendChild(el("span", null, text + " "));
    line.appendChild(el("span", "spin", "◌"));
    if (state.streamEl) turn.insertBefore(line, state.streamEl);
    else turn.appendChild(line);
    scrollDown();
  }

  function stopSpinners() {
    ensureTurn().querySelectorAll(".spin").forEach(function (s) { s.remove(); });
  }

  function showQuestion(payload) {
    removeQuestion();
    var card = el("div", "question-card");
    card.appendChild(el("div", "q", payload.question));
    var opts = el("div", "opts");
    (payload.options || []).forEach(function (option) {
      var b = el("button", null, option);
      b.type = "button";
      b.addEventListener("click", function () {
        disableCard(card);
        sendAnswer(payload.question_id, option);
      });
      opts.appendChild(b);
    });
    card.appendChild(opts);
    var other = el("div", "other");
    var input = el("input");
    input.placeholder = "Other…";
    var send = el("button", null, "Send");
    send.type = "button";
    send.addEventListener("click", function () {
      if (!input.value.trim()) return;
      disableCard(card);
      sendAnswer(payload.question_id, input.value.trim());
    });
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); send.click(); }
    });
    other.appendChild(input);
    other.appendChild(send);
    card.appendChild(other);
    ensureTurn().appendChild(card);
    state.questionCard = card;
    scrollDown();
  }

  function disableCard(card) {
    card.querySelectorAll("button, input").forEach(function (n) { n.disabled = true; });
  }

  function removeQuestion() {
    if (state.questionCard) { state.questionCard.remove(); state.questionCard = null; }
  }

  function addAnswerChip(text) {
    removeQuestion();
    threadInner.appendChild(el("div", "answer-chip", text));
    state.turn = null; // answer splits the turn visually; next activity opens a new bubble
    state.streamEl = null;
    scrollDown();
  }

  function addNotice(text, cls) {
    threadInner.appendChild(el("p", cls || "muted small", text));
    scrollDown();
  }

  function finalize(answer, status) {
    stopSpinners();
    clearStream();
    if (answer) freezeText(answer);
    else if (status !== "done") addNotice("Analysis " + status + ".", "error");
    state.turn = null;
    state.analysisId = null;
    setComposer(true);
    refreshSidebar();
  }

  function setComposer(enabled) {
    questionBox.disabled = !enabled;
    sendBtn.disabled = !enabled;
  }

  function refreshSidebar() {
    if (!state.conversationId) return;
    var list = document.getElementById("conv-list");
    if (!list.querySelector('a[href$="' + state.conversationId + '"]')) {
      var li = document.createElement("li");
      var a = el("a", "active", questionBox.dataset.lastTitle || "New conversation");
      a.href = "/chat?conversation=" + encodeURIComponent(state.conversationId);
      li.appendChild(a);
      list.prepend(li);
    }
  }

  // ---- network ----
  function dispatchEvent_(e) {
    if (e.kind === "assistant") freezeText(e.text);
    else if (e.kind === "step" || e.kind === "error") addToolLine(e.text, e.kind);
    else if (e.kind === "question") {
      try { showQuestion(JSON.parse(e.text)); } catch (err) { /* ignore bad payload */ }
    } else if (e.kind === "answer") addAnswerChip(e.text);
    else if (e.kind === "lifecycle" && e.text !== "Analysis complete") addToolLine(e.text, "lifecycle");
  }

  function poll() {
    if (!state.analysisId) return;
    fetch("/chat/analyses/" + encodeURIComponent(state.analysisId) + "/events?after=" + state.after,
          { credentials: "same-origin" })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (d) {
        (d.events || []).forEach(function (e) { state.after = e.seq; dispatchEvent_(e); });
        if (d.partial) setStreaming(d.partial);
        if (d.status === "running" || d.status === "awaiting_input") {
          setTimeout(poll, 1000);
        } else if (d.status === "stale") {
          stopSpinners(); clearStream();
          addNotice("This analysis appears to have stopped (the server may have restarted). Please ask again.", "error");
          state.analysisId = null;
          setComposer(true);
        } else {
          finalize(d.answer, d.status);
        }
      })
      .catch(function () { setTimeout(poll, 4000); });
  }

  function submit() {
    var question = questionBox.value.trim();
    if (!question || state.analysisId) return;
    var fd = new FormData();
    fd.append("question", question);
    if (state.conversationId) fd.append("conversation", state.conversationId);
    state.attachments.forEach(function (f) { fd.append("files", f, f.name); });

    setComposer(false);
    fetch("/chat/ask", { method: "POST", body: fd, credentials: "same-origin" })
      .then(function (r) {
        return r.json().then(function (d) { return { ok: r.ok, data: d }; });
      })
      .then(function (res) {
        if (!res.ok) {
          addNotice(res.data.error || "Something went wrong.", "error");
          setComposer(true);
          return;
        }
        var names = state.attachments.map(function (f) { return f.name; });
        addUserBubble(question + (names.length ? "\n📎 " + names.join(", ") : ""));
        questionBox.dataset.lastTitle = question.slice(0, 60);
        questionBox.value = "";
        state.attachments = [];
        renderChips();
        state.conversationId = res.data.conversation_id;
        state.analysisId = res.data.analysis_id;
        state.after = 0;
        state.turn = null;
        poll();
      })
      .catch(function () {
        addNotice("Network error — please try again.", "error");
        setComposer(true);
      });
  }

  function sendAnswer(questionId, answer) {
    var fd = new FormData();
    fd.append("question_id", questionId);
    fd.append("answer", answer);
    fetch("/chat/analyses/" + encodeURIComponent(state.analysisId) + "/answer",
          { method: "POST", body: fd, credentials: "same-origin" })
      .catch(function () { /* poll keeps running; the card stays for retry via page state */ });
  }

  // ---- attachments ----
  function addFiles(fileList) {
    Array.prototype.slice.call(fileList).forEach(function (f) {
      if (state.attachments.length >= 5) return;
      if (state.attachments.some(function (a) { return a.name === f.name; })) return;
      state.attachments.push(f);
    });
    renderChips();
  }

  function renderChips() {
    chips.textContent = "";
    state.attachments.forEach(function (f, i) {
      var chip = el("span", "chip", f.name + " ");
      var x = el("button", null, "×");
      x.type = "button";
      x.addEventListener("click", function () {
        state.attachments.splice(i, 1);
        renderChips();
      });
      chip.appendChild(x);
      chips.appendChild(chip);
    });
  }

  document.getElementById("attach-btn").addEventListener("click", function () { fileInput.click(); });
  fileInput.addEventListener("change", function () { addFiles(fileInput.files); fileInput.value = ""; });
  ["dragover", "dragenter"].forEach(function (evt) {
    dropZone.addEventListener(evt, function (e) { e.preventDefault(); dropZone.classList.add("dropping"); });
  });
  ["dragleave", "drop"].forEach(function (evt) {
    dropZone.addEventListener(evt, function (e) { e.preventDefault(); dropZone.classList.remove("dropping"); });
  });
  dropZone.addEventListener("drop", function (e) {
    if (e.dataTransfer && e.dataTransfer.files.length) addFiles(e.dataTransfer.files);
  });

  form.addEventListener("submit", function (e) { e.preventDefault(); submit(); });
  questionBox.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); }
  });

  // ---- boot: resume watching after a reload (PRG / noscript upgrade) ----
  if (app.dataset.watch) {
    state.analysisId = app.dataset.watch;
    state.after = 0;
    setComposer(false);
    poll();
  }
})();
