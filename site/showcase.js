/* "See it work": the feature clips under the hero, played one after another, and the demo in a lightbox.
   Only the clip on screen is loaded. Nothing plays while the player is off the screen, the browser tab is hidden or the
   demo is open, and with reduced motion nothing starts by itself: the poster shows, with a play button. */
(function () {
  "use strict";
  var reduce = window.matchMedia ? matchMedia("(prefers-reduced-motion: reduce)") : { matches: false };
  var demoOpen = false;
  var players = [];

  function Showcase(root) {
    var tabs = [].slice.call(root.querySelectorAll('[role="tab"]'));
    var clips = [].slice.call(root.querySelectorAll("video"));
    var caps = [].slice.call(root.querySelectorAll(".sc-cap"));
    var strip = root.querySelector('[role="tablist"]'), panel = root.querySelector('[role="tabpanel"]');
    var play = root.querySelector(".sc-play"), pause = root.querySelector(".sc-toggle");
    var i = 0, want = !reduce.matches, seen = !window.IntersectionObserver, raf = 0, settle = 0;

    function bar(j) { return tabs[j].querySelector(".bar > span"); }
    function arm(v) {  // the poster and the clip, the first time this one is chosen
      if (!v.getAttribute("poster") && v.dataset.poster) v.poster = v.dataset.poster;
      if (!v.getAttribute("src")) v.src = v.dataset.src;
    }
    function reveal(v) {  // fade the chosen clip in over the one before, which stays until it's covered
      if (v.classList.contains("on")) return;
      clips.forEach(function (c) { c.classList.toggle("was", c.classList.contains("on")); c.classList.remove("on"); });
      v.classList.add("on");
      clearTimeout(settle);
      settle = setTimeout(function () { clips.forEach(function (c) { if (c !== v) c.classList.remove("was"); }); }, 700);
    }
    function center(t) {  // phones: keep the chosen tab in view in the sideways strip
      if (strip.scrollWidth <= strip.clientWidth + 1) return;
      var left = t.offsetLeft - (strip.clientWidth - t.offsetWidth) / 2;
      if (strip.scrollTo) strip.scrollTo({ left: left, behavior: reduce.matches ? "auto" : "smooth" });
      else strip.scrollLeft = left;
    }
    function edges() {  // fade the side of the strip that has more tabs past it
      var x = strip.scrollLeft, room = strip.scrollWidth - strip.clientWidth;
      strip.classList.toggle("more-left", x > 2);
      strip.classList.toggle("more-right", x < room - 2);
    }
    function choose(n, focus) {
      var old = clips[i];
      i = (n + tabs.length) % tabs.length;
      var v = clips[i];
      tabs.forEach(function (t, j) {
        t.setAttribute("aria-selected", j === i);
        t.tabIndex = j === i ? 0 : -1;
        bar(j).style.transform = "";
      });
      caps.forEach(function (c, j) { c.classList.toggle("on", j === i); });
      panel.setAttribute("aria-labelledby", tabs[i].id);
      panel.setAttribute("aria-describedby", caps[i].id);
      if (focus) tabs[i].focus();
      center(tabs[i]);
      if (old !== v) old.pause();
      arm(v);
      try { v.currentTime = 0; } catch (e) { /* not loaded yet: it starts at 0 anyway */ }
      if (!want || v.readyState >= 2) reveal(v);  // a still shows its poster at once; a clip once it plays
      sync();
    }
    function frame() {  // the line under the playing tab
      raf = 0;
      var v = clips[i];
      if (v.duration) bar(i).style.transform = "scaleX(" + Math.min(1, v.currentTime / v.duration).toFixed(4) + ")";
      if (!v.paused && !v.ended) raf = requestAnimationFrame(frame);
    }
    function sync() {
      var v = clips[i], go = want && seen && !document.hidden && !demoOpen;
      root.classList.toggle("is-held", !want);
      play.hidden = want;
      if (go && v.paused) {
        var p = v.play();
        if (p && p.catch) p.catch(function (e) {  // autoplay refused (data saver, a browser setting): wait for a click
          if (e && e.name === "NotAllowedError" && v === clips[i]) { want = false; reveal(v); sync(); }
        });
      } else if (!go && !v.paused) v.pause();
    }

    clips.forEach(function (v) {
      v.addEventListener("playing", function () { if (v === clips[i]) { reveal(v); if (!raf) raf = requestAnimationFrame(frame); } });
      v.addEventListener("ended", function () {
        if (v !== clips[i]) return;
        if (reduce.matches) { want = false; bar(i).style.transform = ""; sync(); }  // one clip at a time, on request
        else choose(i + 1);
      });
    });
    tabs.forEach(function (t, j) {
      t.addEventListener("click", function () { want = !reduce.matches; choose(j); });
    });
    strip.addEventListener("keydown", function (e) {
      var n = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: tabs.length - 1 }[e.key];
      if (n === undefined) return;
      e.preventDefault();
      want = !reduce.matches;
      choose(n, true);
    });
    play.addEventListener("click", function () { want = true; sync(); pause.focus({ preventScroll: true }); });
    pause.addEventListener("click", function () { want = false; sync(); play.focus({ preventScroll: true }); });
    strip.addEventListener("scroll", edges, { passive: true });
    window.addEventListener("resize", edges);
    edges();
    document.addEventListener("visibilitychange", sync);
    if (reduce.addEventListener) reduce.addEventListener("change", function () { want = !reduce.matches; sync(); });
    if (window.IntersectionObserver) {
      new IntersectionObserver(function (es) { seen = es[es.length - 1].isIntersecting; sync(); }, { threshold: 0.35 }).observe(panel);
    }
    choose(0);
    players.push(sync);
  }

  function Lightbox(box) {
    var video = box.querySelector("video"), opener = null, html = document.documentElement;
    function open(e) {
      if (!box.showModal) return;  // no <dialog>: the link scrolls to the demo further down
      e.preventDefault();
      opener = e.currentTarget;
      html.style.setProperty("--sbw", (window.innerWidth - html.clientWidth) + "px");  // no jump when the scrollbar goes
      html.classList.add("lb-open");
      demoOpen = true;
      players.forEach(function (s) { s(); });
      if (!video.getAttribute("src")) video.src = video.dataset.src;
      box.classList.remove("closing");
      box.showModal();
      try { video.currentTime = 0; } catch (x) { /* not loaded yet */ }
      var p = video.play();
      if (p && p.catch) p.catch(function () {});
    }
    function close() {
      if (!box.open || box.classList.contains("closing")) return;
      video.pause();
      if (reduce.matches) return box.close();
      box.classList.add("closing");
      setTimeout(function () { box.close(); }, 170);
    }
    box.addEventListener("cancel", function (e) { e.preventDefault(); close(); });  // Esc
    box.addEventListener("click", function (e) {  // the backdrop, the space around the player, or the X
      if (e.target === box || e.target.closest("[data-close]")) close();
    });
    box.addEventListener("close", function () {
      box.classList.remove("closing");
      video.pause();
      html.classList.remove("lb-open");
      demoOpen = false;
      players.forEach(function (s) { s(); });
      if (opener) opener.focus();
    });
    [].forEach.call(document.querySelectorAll("[data-watch]"), function (a) { a.addEventListener("click", open); });
  }

  function start() {
    [].forEach.call(document.querySelectorAll("[data-showcase]"), Showcase);
    var box = document.getElementById("watch");
    if (box) Lightbox(box);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
