/* Match 3 ↔ 3 and Match 6 ↔ 6 — pairing faces with names, by drag or by two taps.
 *
 * Both gestures are first class (§6): drag with a pointer (mouse, finger or pen — one code path,
 * since pointer events cover all three), or tap a name and then tap a face. Nothing here knows
 * which one you used, and neither does the server: the board's answer is a mapping.
 *
 * A misplaced name is the interesting signal — it names two people at once — so a placement is
 * never silently overwritten: taking a chip that is already on a face puts it back in the tray.
 */
(function () {
  "use strict";

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function render(container, payload, onSubmit) {
    var places = {};
    var tray = el("div", "board-names");
    var board = el("div", "board-faces");
    var chips = {};
    var held = null;
    var drag = null;

    function faceFor(personId) {
      return board.querySelector('[data-face="' + personId + '"]');
    }

    function release(chip) {
      var slot = chip.parentElement;
      if (slot && slot.classList.contains("face-slot")) {
        slot.classList.remove("filled");
        delete places[slot.getAttribute("data-face")];
      }
      tray.appendChild(chip);
      chip.classList.remove("held", "placed");
      chip.setAttribute("aria-pressed", "false");
    }

    function place(chip, personId) {
      var slot = faceFor(personId);
      if (!slot || chip.parentElement === slot) return;
      var occupant = slot.querySelector(".name-chip");
      if (occupant && occupant !== chip) release(occupant);
      release(chip);
      slot.appendChild(chip);
      slot.classList.add("filled");
      chip.classList.add("placed");
      places[personId] = chip.getAttribute("data-name");
      if (held === chip) hold(null);
      refresh();
    }

    function hold(chip) {
      if (held && held !== chip) {
        held.classList.remove("held");
        held.setAttribute("aria-pressed", "false");
      }
      held = chip;
      if (held) {
        held.classList.add("held");
        held.setAttribute("aria-pressed", "true");
      }
    }

    function complete() {
      return Object.keys(places).length === (payload.faces || []).length;
    }

    function refresh() {
      check.disabled = !complete();
      note.textContent = complete()
        ? "All placed — check your answer when you are ready."
        : Object.keys(places).length + " of " + (payload.faces || []).length + " placed";
    }

    // ── the tray: one chip per name, in its own order ────────────────────────
    (payload.names || []).forEach(function (name) {
      var chip = el("button", "name-chip", name.label);
      chip.type = "button";
      chip.setAttribute("data-name", String(name.person_id));
      chip.setAttribute("aria-pressed", "false");
      chip.addEventListener("click", function () {
        if (drag) return;                       // a drag just ended here; not a tap
        if (held === chip) { hold(null); return; }
        if (chip.classList.contains("placed")) { release(chip); refresh(); return; }
        hold(chip);
      });
      chip.addEventListener("pointerdown", function (event) {
        startDrag(event, chip);
      });
      chips[name.person_id] = chip;
      tray.appendChild(chip);
    });

    // ── the faces: a drop target, and a tap target for the chip in hand ──────
    (payload.faces || []).forEach(function (face) {
      var slot = el("div", "face-slot");
      slot.setAttribute("data-face", String(face.person_id));
      if (face.photo_url) {
        var image = el("img", "face-photo");
        image.src = face.photo_url;
        image.alt = "";                         // the name is the answer; the face is the question
        image.draggable = false;
        slot.appendChild(image);
      }
      slot.appendChild(el("span", "face-empty", "?"));
      var button = el("button", "face-hit", "");
      button.type = "button";
      button.setAttribute("aria-label", "face");
      button.addEventListener("click", function () {
        if (held) { place(held, slot.getAttribute("data-face")); }
      });
      slot.appendChild(button);
      board.appendChild(slot);
    });

    var note = el("p", "hint muted small", "");
    var check = el("button", "btn primary", "Check");
    check.type = "button";
    check.addEventListener("click", function () {
      if (complete() && typeof onSubmit === "function") onSubmit(places);
    });

    container.appendChild(el("p", "prompt", payload.prompt || "Match each face to their name"));
    var layout = el("div", "board");
    layout.appendChild(board);
    layout.appendChild(tray);
    container.appendChild(layout);
    container.appendChild(note);
    container.appendChild(check);
    refresh();

    // ── drag: a pointer, a floating copy, and whatever face it was let go over ──
    function startDrag(event, chip) {
      if (event.button !== undefined && event.button !== 0) return;
      drag = null;
      var startX = event.clientX;
      var startY = event.clientY;
      var ghost = null;
      var moved = false;

      function move(moveEvent) {
        if (!moved) {
          if (Math.abs(moveEvent.clientX - startX) < 6 && Math.abs(moveEvent.clientY - startY) < 6) return;
          moved = true;
          ghost = chip.cloneNode(true);
          ghost.classList.add("ghost");
          document.body.appendChild(ghost);
          chip.classList.add("dragging");
          hold(null);
        }
        ghost.style.left = moveEvent.clientX + "px";
        ghost.style.top = moveEvent.clientY + "px";
      }

      function up(upEvent) {
        document.removeEventListener("pointermove", move);
        document.removeEventListener("pointerup", up);
        if (ghost) ghost.remove();
        chip.classList.remove("dragging");
        if (!moved) { drag = null; return; }
        var over = document.elementFromPoint(upEvent.clientX, upEvent.clientY);
        var slot = over && over.closest ? over.closest(".face-slot") : null;
        if (slot) place(chip, slot.getAttribute("data-face"));
        drag = null;
        // Swallow the click that follows a drag, so letting go over a face is not also a tap.
        chip.addEventListener("click", function (event) { event.stopPropagation(); }, { once: true });
      }

      document.addEventListener("pointermove", move);
      document.addEventListener("pointerup", up);
      drag = true;
      if (event.preventDefault) event.preventDefault();
    }
  }

  window.KithMatch = { render: render };
})();
