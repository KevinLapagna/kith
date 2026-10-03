/* Kith trainer — the one screen in the app that needs JavaScript.
 *
 * Everything authoritative is on the server: this file renders a payload it is handed and
 * posts back the tap you made. It never grades, never invents a schedule, and never sees an
 * answer before you have given one.
 *
 * It is written as a plain script against the DOM on purpose: no framework, no build step,
 * nothing to install and nothing to keep up to date. The whole state machine is below.
 */
(function () {
  "use strict";

  var root = document.getElementById("trainer");
  var bootEl = document.getElementById("kith-bootstrap");
  if (!root || !bootEl) return;

  var sessionId = root.getAttribute("data-session");
  var stage = document.getElementById("stage");
  var feedback = document.getElementById("feedback");
  var bar = document.getElementById("progress-bar");
  var progressText = document.getElementById("progress-text");

  var state = {
    envelope: null,
    payload: null,
    shownAt: 0,
    revealed: false,
    hintLevel: 0,
    busy: false,
    finished: false
  };

  try {
    state.envelope = JSON.parse(bootEl.textContent);
  } catch (err) {
    stage.textContent = "This session could not be started.";
    return;
  }
  // The bootstrap is a snapshot of the first prompt, and a study card names the person.
  // Drop it from the DOM once parsed: nothing on the page should still hold an answer to
  // a prompt that is still to come.
  bootEl.textContent = "";

  // ── tiny DOM helpers ───────────────────────────────────────────────────────
  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  // A name with the family name riding along in a lighter, smaller weight: it is learned on
  // the side, never asked for, so it must not compete with the answer. Skipped when the asked
  // name already contains it, so "Rafa Nadal" never reads as "Rafa Nadal Nadal".
  function nameLine(tag, className, name, family) {
    var node = el(tag, className, name);
    var extra = String(family || "").trim();
    if (extra && String(name || "").toLowerCase().indexOf(extra.toLowerCase()) === -1) {
      node.appendChild(document.createTextNode(" "));
      node.appendChild(el("span", "name-family", extra));
    }
    return node;
  }

  function photo(url, name, className) {
    var frame = el("div", "photo " + (className || ""));
    if (!url) {
      frame.appendChild(el("div", "photo-missing", name ? name.slice(0, 1) : "?"));
      return frame;
    }
    var img = document.createElement("img");
    img.src = url;
    img.alt = name ? "Photo of " + name : "Photo";
    img.decoding = "async";
    frame.appendChild(img);
    return frame;
  }

  // ── progress ───────────────────────────────────────────────────────────────
  function paintProgress(envelope) {
    var s = envelope.session || {};
    var planned = s.planned || 0;
    var done = s.retired || 0;
    var pct = planned ? Math.min(100, Math.round((done / planned) * 100)) : 0;
    if (bar) bar.style.width = pct + "%";
    if (progressText) {
      progressText.textContent = planned
        ? done + " of " + planned + (s.answers ? " · " + s.correct + "/" + s.answers + " right" : "")
        : (s.answers || 0) + " answered";
    }
  }

  // ── rendering one prompt ───────────────────────────────────────────────────
  function render(payload) {
    state.payload = payload;
    state.revealed = false;
    state.hintLevel = 0;
    // Your confidence belongs to the prompt you were looking at, not to the run.
    state.confidence = null;
    // Publish what is on screen: makes the screen testable from the outside, and tells a
    // stylesheet (or a future keyboard hint) which mode is asking.
    root.setAttribute("data-mode", payload.mode || "");
    root.setAttribute("data-item", String(payload.item_id || ""));
    root.setAttribute("data-direction", payload.direction || "");
    if (payload.requeues) {
      root.setAttribute("data-requeues", String(payload.requeues));
    } else {
      root.removeAttribute("data-requeues");
    }
    state.shownAt = Date.now();
    clear(feedback);
    stage.className = "stage";
    clear(stage);

    if (!payload) {
      finish();
      return;
    }

    var card = el("div", "ask ask-" + payload.mode);
    if (payload.requeues) card.appendChild(el("span", "again-badge", "again"));

    // Three shapes of question: the face is named (face→name), the name is the question
    // (name→face, with the name spoken when the mode says so), or a study card, which is
    // shown by renderStudy instead.
    if (payload.board) {
      // A board brings its own faces and names: no single photo, no prompt line.
    } else if (payload.speak) {
      card.appendChild(speakControls(payload));
    } else if (payload.name && payload.direction === "name_to_face") {
      card.appendChild(nameLine("p", "ask-name", payload.name, null));
    } else if (payload.photo_url || payload.mode !== "study") {
      card.appendChild(photo(payload.photo_url, null, "big"));
    }
    if (!payload.board) card.appendChild(el("p", "prompt", payload.prompt || ""));

    if (payload.mode === "study") {
      renderStudy(card, payload);
    } else if (payload.board) {
      window.KithMatch.render(card, payload, function (placements) {
        submit({ item_id: payload.item_id, matching: placements, latency_ms: elapsed() });
      });
    } else if (payload.hintable) {
      renderRecall(card, payload);
    } else {
      renderChoice(card, payload);
    }

    // Only where there is a real answer to be sure *about*: free recall already asks you to
    // rate yourself, and a study card asks nothing at all.
    if (payload.mode !== "study" && !payload.hintable) card.appendChild(confidenceRow());

    stage.appendChild(card);
    var first = stage.querySelector("button");
    if (first) first.focus();
  }

  // ── speaking a name (listening mode) ───────────────────────────────────────
  // The browser does the talking: nothing is bundled, nothing is downloaded, and where a
  // browser cannot speak the question is still answerable — the name can simply be shown.
  function speak(text) {
    if (!text || !window.speechSynthesis || typeof SpeechSynthesisUtterance !== "function") {
      return false;
    }
    try {
      window.speechSynthesis.cancel();
      var utterance = new SpeechSynthesisUtterance(String(text));
      utterance.rate = 0.9;
      utterance.lang = document.documentElement.lang || "en";
      window.speechSynthesis.speak(utterance);
      return true;
    } catch (err) {
      return false;
    }
  }

  function speakControls(payload) {
    var box = el("div", "speak");
    if (!speak(payload.speak)) {
      box.appendChild(el("p", "hint muted small", "This browser cannot speak the name."));
    }
    var again = el("button", "btn small ghost", "Say it again");
    again.type = "button";
    again.addEventListener("click", function () { speak(payload.speak); });
    box.appendChild(again);

    var showName = el("button", "btn small ghost", "Show the name");
    showName.type = "button";
    showName.addEventListener("click", function () {
      if (!box.parentNode) return;
      box.parentNode.insertBefore(nameLine("p", "ask-name", payload.name, null), box);
      showName.remove();
    });
    box.appendChild(showName);
    return box;
  }

  function renderStudy(container, payload) {
    var info = el("div", "study-card");
    info.appendChild(nameLine("p", "study-name", payload.card.name, payload.card.family_name));
    if (payload.card.mnemonic) info.appendChild(el("p", "mnemonic", payload.card.mnemonic));
    var meta = [payload.card.where_met, payload.card.feature_note].filter(Boolean).join(" · ");
    if (meta) info.appendChild(el("p", "meta", meta));
    container.appendChild(info);

    var go = el("button", "btn primary big", "Got it — next");
    go.type = "button";
    go.addEventListener("click", function () {
      submit({ item_id: payload.item_id, ack: true });
    });
    container.appendChild(go);
  }

  // ── how sure you were ──────────────────────────────────────────────────────
  // Two faces, deliberately optional, tapped *before* the verdict: a confidence given after
  // you have seen the answer is a comment on it, not a memory of your own certainty. This is
  // the only place the app asks you about yourself, and it is the only thing that can tell
  // you your mistakes were confident ones.
  function confidenceRow() {
    var row = el("div", "confidence-row");
    row.appendChild(el("span", "confidence-ask", "How sure?"));

    [["😐", 1, "I was guessing"], ["😀", 2, "I was sure"]].forEach(function (face) {
      var button = el("button", "btn small ghost", face[0]);
      button.type = "button";
      button.title = face[2];
      button.setAttribute("aria-label", face[2]);
      button.setAttribute("aria-pressed", "false");
      button.addEventListener("click", function () {
        choose(button, face[1]);
      });
      row.appendChild(button);
    });

    // Tapping the face you already chose takes it back: "I would rather not say" has to stay
    // reachable, or the button is a trap.
    function choose(button, value) {
      var already = state.confidence === value;
      Array.prototype.forEach.call(row.querySelectorAll("button"), function (other) {
        other.className = "btn small ghost";
        other.setAttribute("aria-pressed", "false");
      });
      state.confidence = already ? null : value;
      if (!already) {
        button.className = "btn small primary";
        button.setAttribute("aria-pressed", "true");
      }
    }

    return row;
  }

  function renderChoice(container, payload) {
    var list = el("ul", "options" + (payload.direction === "name_to_face" ? " faces" : ""));
    (payload.options || []).forEach(function (option, index) {
      var item = el("li");
      var button = el("button", "option");
      button.type = "button";
      button.appendChild(el("span", "key", String(index + 1)));
      if (option.photo_url) {
        // The answer *is* the face: a caption here would be the answer key. The name comes
        // back with the verdict, once it costs nothing.
        var tile = photo(option.photo_url, null, "option-photo");
        tile.setAttribute("aria-hidden", "true");
        button.appendChild(tile);
        button.setAttribute("aria-label", "Face " + (index + 1));
      } else {
        button.appendChild(el("span", "label", option.label));
      }
      button.addEventListener("click", function () {
        submit({ item_id: payload.item_id, person_id: option.person_id, latency_ms: elapsed() });
      });
      item.appendChild(button);
      list.appendChild(item);
    });
    container.appendChild(list);
  }

  function renderRecall(container, payload) {
    var hints = el("div", "hints");
    container.appendChild(hints);

    var reveal = el("button", "btn primary big", "Reveal");
    reveal.type = "button";
    reveal.addEventListener("click", doReveal);
    container.appendChild(reveal);

    var hint = el("button", "btn small ghost", "Hint (H)");
    hint.type = "button";
    hint.addEventListener("click", askHint);
    container.appendChild(hint);

    var noIdea = el("button", "btn small ghost", "Never seen them");
    noIdea.type = "button";
    noIdea.addEventListener("click", function () {
      submit({
        item_id: payload.item_id,
        no_idea: true,
        hint_level: state.hintLevel,
        latency_ms: elapsed()
      });
    });
    container.appendChild(noIdea);

    container._hints = hints;
    container._revealButton = reveal;
    container._hintButton = hint;
  }

  function doReveal() {
    var payload = state.payload;
    if (!payload || !payload.hintable || state.revealed) return;
    // The answer is not in the page while you are still trying to produce it.
    post("/api/sessions/" + sessionId + "/reveal", { item_id: payload.item_id })
      .then(function (data) {
        if (!data || !data.ok || !data.reveal) {
          showError((data && data.error) || "Could not reveal that card.");
          return;
        }
        paintReveal(data.reveal);
      });
  }

  function paintReveal(reveal) {
    var payload = state.payload;
    if (!payload || state.revealed) return;
    state.revealed = true;

    var card = stage.querySelector(".ask");
    if (!card) return;

    var block = el("div", "reveal");
    block.appendChild(nameLine("p", "reveal-name", reveal.name, reveal.family_name));
    if (reveal.mnemonic) block.appendChild(el("p", "mnemonic", reveal.mnemonic));
    var meta = [reveal.where_met, reveal.feature_note].filter(Boolean).join(" · ");
    if (meta) block.appendChild(el("p", "meta", meta));
    card.appendChild(block);
    card.classList.add("is-revealed");

    var ratings = el("div", "ratings");
    var labels = ["Didn't get it", "Hard", "Good", "Easy"];
    labels.forEach(function (label, index) {
      var rating = index + 1;
      var button = el("button", "rate rate-" + rating);
      button.type = "button";
      button.appendChild(el("span", "key", String(rating)));
      button.appendChild(el("span", "label", label));
      button.addEventListener("click", function () {
        submit({
          item_id: payload.item_id,
          self_rating: rating,
          hint_level: state.hintLevel,
          latency_ms: elapsed()
        });
      });
      ratings.appendChild(button);
    });
    card.appendChild(ratings);

    if (card._revealButton) card._revealButton.remove();
    if (card._hintButton) card._hintButton.remove();
    var first = ratings.querySelector("button");
    if (first) first.focus();
  }

  function askHint() {
    var payload = state.payload;
    if (!payload || !payload.hintable) return;
    var next = Math.min(state.hintLevel + 1, payload.hint_count || 5);
    state.hintLevel = next;
    post("/api/sessions/" + sessionId + "/hint", { item_id: payload.item_id, level: next })
      .then(function (data) {
        var card = stage.querySelector(".ask");
        if (!card || !data || !data.ok) return;
        var box = card._hints || card.querySelector(".hints");
        if (box) {
          box.appendChild(el("span", "hint-chip", data.text));
        }
      });
    acknowledgeAnswer();
  }

  function acknowledgeAnswer() {
    if (navigator.vibrate) navigator.vibrate(8);   // a tap you can feel, not just see
  }

  function elapsed() {
    return Math.max(0, Date.now() - state.shownAt);
  }

  // ── talking to the server ──────────────────────────────────────────────────
  function post(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify(body)
    }).then(function (response) {
      return response.json().catch(function () {
        return { ok: false, error: "unreadable response" };
      });
    });
  }

  function submit(answer) {
    if (state.busy || state.finished) return;
    state.busy = true;
    if (state.confidence) answer.confidence = state.confidence;
    acknowledgeAnswer();
    post("/api/sessions/" + sessionId + "/answer", answer)
      .then(function (data) {
        state.busy = false;
        if (!data || !data.ok) {
          showError((data && data.error) || "Could not save that answer.");
          return;
        }
        state.envelope = data;
        paintProgress(data);
        showVerdict(data.verdict, data.next);
      })
      .catch(function () {
        state.busy = false;
        showError("The connection dropped. Your answer was not saved — tap again.");
      });
  }

  function showError(message) {
    clear(feedback);
    var box = el("p", "notice err", message);
    feedback.appendChild(box);
  }

  function showVerdict(verdict, next) {
    if (!verdict) {
      render(next);
      return;
    }
    if (verdict.board) {
      showBoardVerdict(verdict, next);
      return;
    }
    if (verdict.correct) {
      stage.className = "stage right";
    } else {
      stage.className = "stage wrong";
    }

    clear(feedback);
    if (!verdict.correct) {
      var box = el("div", "verdict wrong");
      box.appendChild(nameLine("p", "verdict-line", "That was " + verdict.correct_name, verdict.correct_family));
      if (verdict.mnemonic) box.appendChild(el("p", "mnemonic", verdict.mnemonic));
      if (verdict.where_met) box.appendChild(el("p", "meta", verdict.where_met));
      feedback.appendChild(box);
    } else {
      feedback.appendChild(el("div", "verdict right", verdict.rating ? "✓" : "✓"));
    }

    state.next = next;
    var advanceButton = el("button", "btn primary", "Continue (Enter)");
    advanceButton.type = "button";
    advanceButton.addEventListener("click", advance);
    feedback.appendChild(advanceButton);
    advanceButton.focus();

    // A right answer should not need a second tap if you are already moving on.
    if (verdict.correct) {
      window.setTimeout(function () {
        if (state.next === next && !state.finished) advance();
      }, 850);
    }
  }

  // A board is several verdicts at once: every face, and who was swapped with whom. No
  // auto-advance here — with three to six lines to read, 850ms is not enough time to find out
  // what you got wrong, and the slip *is* the useful part.
  function showBoardVerdict(verdict, next) {
    stage.className = verdict.correct ? "stage right" : "stage wrong";
    clear(feedback);
    var box = el("div", "verdict board");
    box.appendChild(
      el("p", "verdict-line", verdict.correct ? "Every name in the right place." : "These were swapped:")
    );
    var list = el("ul", "board-results");
    (verdict.results || []).forEach(function (result) {
      var row = el("li", result.correct ? "right" : "wrong");
      row.appendChild(nameLine("span", "board-name", result.name, result.family_name));
      if (!result.correct) {
        row.appendChild(
          el("span", "board-slip", result.chosen_name ? " — you put " + result.chosen_name + " here" : " — still wrong")
        );
      }
      list.appendChild(row);
    });
    box.appendChild(list);
    feedback.appendChild(box);
    state.next = next;
    var button = el("button", "btn primary", "Continue (Enter)");
    button.type = "button";
    button.addEventListener("click", advance);
    feedback.appendChild(button);
    button.focus();
  }

  function advance() {
    if (state.finished) return;
    if (!state.next) {
      finish();
      return;
    }
    render(state.next);
  }

  function finish() {
    if (state.finished) return;
    state.finished = true;
    post("/api/sessions/" + sessionId + "/end", {}).then(function () {
      window.location.href = "/train/" + sessionId + "/summary";
    });
  }

  // ── keyboard: the whole session without a mouse ────────────────────────────
  document.addEventListener("keydown", function (event) {
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    var tag = (event.target && event.target.tagName) || "";
    if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return;

    var key = event.key;

    if (key === "Enter") {
      if (feedback.querySelector("button")) {
        event.preventDefault();
        advance();
      } else if (state.payload && state.payload.hintable && !state.revealed) {
        event.preventDefault();
        doReveal();
      }
      return;
    }
    if (key === " " || key === "Spacebar") {
      if (state.payload && state.payload.hintable && !state.revealed) {
        event.preventDefault();
        doReveal();
      }
      return;
    }
    if (key === "h" || key === "H") {
      event.preventDefault();
      askHint();
      return;
    }
    if (key >= "1" && key <= "4") {
      var index = parseInt(key, 10) - 1;
      var buttons = stage.querySelectorAll("button.option, button.rate");
      if (index < buttons.length) {
        event.preventDefault();
        buttons[index].click();
      }
    }
  });

  if (document.getElementById("end-session")) {
    document.getElementById("end-session").addEventListener("click", function () {
      if (window.confirm("End this session and see the summary?")) {
        finish();
      }
    });
  }

  // A tab hidden mid-prompt should not count as hesitation when it comes back.
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) state.shownAt = Date.now();
  });

  paintProgress(state.envelope);
  render(state.envelope.next);
})();
