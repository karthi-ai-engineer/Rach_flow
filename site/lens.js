/* The Rflow lens: what you say drifts in on the left, passes through the lens (the steps Rflow really takes) and comes
   out clean on the right, typed into a box. Four examples, all true to Rflow. Builds itself inside #lens (or any
   [data-lens]); without JavaScript lens.css shows the first example still. Pauses off-screen and in hidden tabs. */
(function () {
  "use strict";
  // say: the words; _filler, !misheard word, ^number, *name kept (sel: text selected or copied, not spoken).
  // clean: the text typed, {highlighted}. chips: colour token, then the label.
  var DICT = '<span>Hold <kbd>Ctrl</kbd>+<kbd>Win</kbd> and speak</span>';
  var EX = [
    {tab: "Dictation", mode: "Dictation", hint: DICT, cap: "You say", out: "At the cursor", done: "Typed", wave: 1,
      say: "_um so tell *Priya the !post !grass migration is _uh done and _like ^twenty ^five ^percent of the users are on it",
      clean: "Tell {Priya} the {PostgreSQL} migration is done, and {25%} of the users are on it.",
      chips: ["vi:Filler removed", "ir:Your word: PostgreSQL", "wr:Number: 25%", "okc:Name kept: Priya"]},
    {tab: "Numbers", mode: "Dictation", hint: DICT, cap: "You say", out: "At the cursor", done: "Typed", wave: 1,
      say: "the budget is ^twenty ^five ^thousand ^dollars and the review is at ^three ^thirty ^pm",
      clean: "The budget is {$25,000} and the review is at {3:30 PM}.",
      chips: ["wr:Money: $25,000", "wr:Time: 3:30 PM"]},
    {tab: "Concise", mode: "Text Transform", hint: "<span>Select, then say <b>\u201cmake it concise\u201d</b></span>",
      cap: "Selected", out: "In its place", done: "Replaced", wave: 1, sel: 1,
      say: "I checked the deployment and everything looks good, but we still have one issue with the database migration, " +
        "and I think we should fix that before production.",
      clean: "Deployment looks good, but the database migration issue needs to be fixed before production.",
      chips: ["vi:Make it concise", "okc:Facts kept"]},
    {tab: "Translate", mode: "Translate", hint: "<span>Select, then <kbd>Ctrl</kbd>+<kbd>C</kbd> twice</span>",
      cap: "Copied", out: "Popup", done: "Copy or Replace", lang: "ja", sel: 1,
      say: "\u6765\u9031\u306e\u5b9a\u4f8b\u4f1a\u8b70\u306f\u6728\u66dc\u65e5\u306e\u5348\u5f8c3\u6642\u304b\u3089\u306b" +
        "\u5909\u66f4\u306b\u306a\u308a\u307e\u3057\u305f\u3002",
      clean: "Next week\u2019s regular meeting has moved to Thursday at {3 PM}.",
      chips: ["ir:Japanese \u2192 English"]}
  ];
  var CHECK = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>';
  var KIND = {_: "f", "!": "x", "^": "n", "*": "k"};

  function el(tag, cls, html) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html) e.innerHTML = html;
    return e;
  }
  function stack(cls, parts) {
    var s = el("div", "lens-stack " + cls);
    parts.forEach(function (p) { s.appendChild(typeof p === "string" ? el("span", "", p) : p); });
    return s;
  }
  // The clean text typed up to k characters: the rest is there but hidden, so the box never changes size.
  function typed(segs, k) {
    var shown = "", rest = "", n = 0;
    segs.forEach(function (s) {
      var a = Math.max(0, Math.min(s.t.length, k - n)), o = s.b ? "<b>" : "", c = s.b ? "</b>" : "";
      n += s.t.length;
      if (a) shown += o + s.t.slice(0, a) + c;
      if (a < s.t.length) rest += o + s.t.slice(a) + c;
    });
    return shown + '<i class="lens-caret"></i>' + (rest ? "<s>" + rest + "</s>" : "");
  }

  function Lens(root) {
    var label = root.getAttribute("aria-label") || "";
    var view = el("div", "lens-view"), tabs = el("div", "lens-tabs");
    view.setAttribute("role", "img");
    root.removeAttribute("role");
    root.removeAttribute("aria-label");
    tabs.setAttribute("role", "group");
    tabs.setAttribute("aria-label", "Examples");

    var X = EX.map(function (d) {  // this lens's own copy: a page may have more than one (lens-preview.html)
      var ex = Object.create(d), p = el("p");
      if (ex.lang) p.lang = ex.lang;
      ex.w = (ex.sel ? [ex.say] : ex.say.split(" ")).map(function (t, i) {
        var k = ex.sel ? "s" : KIND[t[0]], w = el("span", "w" + (k ? " " + k : ""));
        w.textContent = k && !ex.sel ? t.slice(1) : t;
        // a little messy, the same way every time
        w.style.cssText = "--dx:" + (Math.sin(i * 1.3) * .8).toFixed(1) + "px;--dy:" + (Math.sin(i * 2.7) * 2).toFixed(1) +
          "px;--r:" + (Math.sin(i * 4.1) * .7).toFixed(2) + "deg";
        if (i) p.appendChild(document.createTextNode(" "));
        return p.appendChild(w);
      });
      ex.p = p;
      ex.segs = ex.clean.split(/(\{[^}]*\})/).filter(Boolean).map(function (s) {
        return s[0] === "{" ? {t: s.slice(1, -1), b: 1} : {t: s};
      });
      ex.len = ex.segs.reduce(function (n, s) { return n + s.t.length; }, 0);
      ex.text = el("p", "", typed(ex.segs, ex.len));
      ex.chipBox = el("div");
      ex.c = ex.chips.map(function (c) {
        var at = c.indexOf(":"), chip = el("span", "c");
        chip.textContent = c.slice(at + 1);
        chip.style.setProperty("--k", "var(--" + c.slice(0, at) + ")");
        return ex.chipBox.appendChild(chip);
      });
      // the timeline, in ms: words in, the lens at work (P), typing (O to done), a pause, then the next example
      var n = ex.w.length, s = Math.min(120, 1500 / n), P = 300 + (n - 1) * s + (ex.sel ? 1500 : 700), type = Math.min(2200, ex.len * 26);
      var done = P + 800 + type + 150;
      ex.t = {s: s, P: P, O: P + 800, type: type, done: done, gap: Math.min(520, (done - P - 500) / ex.chips.length),
        end: Math.max(6800, done + 2400)};
      ex.btn = el("button", "", ex.tab + "<i></i>");
      ex.btn.type = "button";
      return ex;
    });

    var stacks = [
      stack("lens-mode", X.map(function (ex) { return "<span><i></i>" + ex.mode + "</span>"; })),
      stack("lens-hint", X.map(function (ex) { return ex.hint; })),
      stack("", X.map(function (ex) { return ex.cap; })),
      stack("lens-say", X.map(function (ex) { return ex.p; })),
      stack("", X.map(function (ex) { return ex.out; })),
      stack("lens-text", X.map(function (ex) { return ex.text; })),
      stack("lens-done", X.map(function (ex) { return CHECK + ex.done; })),
      stack("lens-chips", X.map(function (ex) { return ex.chipBox; }))
    ];
    var head = el("div", "lens-head"), stage = el("div", "lens-stage"), inp = el("div", "lens-in"), out = el("div", "lens-out");
    var capIn = el("div", "lens-cap"), capOut = el("div", "lens-cap"), box = el("div", "lens-box");
    head.append(stacks[0], stacks[1]);
    capIn.append(stacks[2], el("span", "lens-wave", "<i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i>"));
    inp.append(capIn, stacks[3]);
    capOut.append(stacks[4]);
    box.append(stacks[5]);
    out.append(capOut, box, stacks[6]);
    stage.append(inp, el("div", "lens-mid", '<i class="lens-pipe"></i><div class="lens-orb"><i class="lens-ring"></i><div class="lens-core">' +
      '<span class="lens-bars"><i></i><i></i><i></i><i></i><i></i></span><span class="lens-dots"><i></i><i></i><i></i></span>' +
      '<span class="lens-check">' + CHECK + '</span></div></div><i class="lens-pipe"></i>'), out, stacks[7]);
    view.append(head, stage);
    stage.querySelectorAll(".lens-wave i, .lens-bars i").forEach(function (b, j) {
      b.style.setProperty("--h", (6 + Math.abs(Math.sin(j * 2.1)) * 12).toFixed(0) + "px");
      b.style.animationDelay = (j * -0.13).toFixed(2) + "s";
    });
    X.forEach(function (ex, j) {
      ex.btn.addEventListener("click", function () { show(j); run(); });
      tabs.appendChild(ex.btn);
    });
    root.textContent = "";
    root.append(view, tabs);

    var reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)");
    var still = root.dataset.motion === "reduce" || !!(reduce && reduce.matches), seen = true, raf = 0, last = 0;
    var i = 0, t = 0, k = -1, frozen = root.dataset.seek != null;

    function flag(name, on) { root.classList.toggle(name, !!on); }
    function show(n) {
      i = (n + X.length) % X.length;
      t = 0;
      k = -1;
      stacks.forEach(function (s) {
        [].forEach.call(s.children, function (c, j) { c.classList.toggle("on", j === i); });
      });
      X.forEach(function (ex, j) {
        ex.btn.setAttribute("aria-pressed", j === i);
        ex.btn.lastChild.style.transform = "";
        if (j !== i) ex.w.concat(ex.c).forEach(function (e) { e.classList.remove("in"); });
      });
      var ex = X[i];
      view.setAttribute("aria-label", label + " Example, " + ex.tab + ": \u201c" + ex.say.replace(/[_!^*]/g, "") +
        "\u201d becomes \u201c" + ex.clean.replace(/[{}]/g, "") + "\u201d");
      flag("lens--still", still);
      draw(still ? 1e9 : 0);
    }
    function draw(now) {
      var ex = X[i], p = ex.t;
      flag("is-wave", ex.wave);
      flag("is-live", ex.wave && now < p.P);
      flag("is-proc", now >= p.P);
      flag("is-type", now >= p.O);
      flag("is-done", now >= p.done);
      flag("is-out", !still && now >= p.end - 500);
      ex.w.forEach(function (w, j) { w.classList.toggle("in", now >= 300 + j * p.s); });
      ex.c.forEach(function (c, j) { c.classList.toggle("in", now >= p.P + 300 + j * p.gap); });
      var nk = Math.max(0, Math.min(ex.len, Math.floor((now - p.O) / p.type * ex.len)));
      if (nk !== k) ex.text.innerHTML = typed(ex.segs, k = nk);
      ex.btn.lastChild.style.transform = still ? "" : "scaleX(" + Math.min(1, now / p.end).toFixed(3) + ")";
    }
    function frame(now) {
      raf = 0;
      t += Math.min(64, now - (last || now));  // after a pause, carry on where it was
      last = now;
      if (t >= X[i].t.end) show(i + 1);
      draw(t);
      run();
    }
    function run() {
      var go = seen && !document.hidden && !still && !frozen;
      flag("is-paused", !go);
      if (go && !raf) raf = requestAnimationFrame(frame);
      if (!go) {
        cancelAnimationFrame(raf);
        raf = last = 0;
      }
    }

    show(+root.dataset.start || 0);
    if (frozen) {  // a fixed moment, for checking (lens-preview.html)
      var at = +root.dataset.seek;
      while (at >= X[i].t.end) {
        at -= X[i].t.end;
        show(i + 1);
      }
      draw(t = at);
    }
    if (reduce && reduce.addEventListener) reduce.addEventListener("change", function () { still = reduce.matches; show(i); run(); });
    document.addEventListener("visibilitychange", run);
    if (window.IntersectionObserver) {
      new IntersectionObserver(function (es) { seen = es[es.length - 1].isIntersecting; run(); }).observe(root);
    }
    run();
  }

  function start() {
    [].forEach.call(document.querySelectorAll("#lens, [data-lens]"), function (root) {
      if (!root.firstChild) Lens(root);
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
