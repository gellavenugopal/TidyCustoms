// Add / remove rows in line-entry tables (imports, BOMs, exports, DTA).
document.addEventListener("click", function (e) {
  var add = e.target.closest("[data-add-row]");
  if (add) {
    var table = document.getElementById(add.getAttribute("data-add-row"));
    var tpl = table.querySelector("tbody tr");
    var row = tpl.cloneNode(true);
    row.querySelectorAll("input").forEach(function (i) { i.value = ""; });
    row.querySelectorAll("select").forEach(function (s) { s.selectedIndex = 0; });
    table.querySelector("tbody").appendChild(row);
    e.preventDefault();
  }
  var del = e.target.closest("[data-del-row]");
  if (del) {
    var body = del.closest("tbody");
    if (body.rows.length > 1) del.closest("tr").remove();
    e.preventDefault();
  }
});
// Chart tooltips: any SVG mark with data-tip shows it on hover / keyboard focus.
(function () {
  var tip = null;
  function show(el, x, y) {
    tip = tip || document.getElementById("viz-tip");
    if (!tip) return;
    tip.textContent = el.getAttribute("data-tip");
    var w = tip.offsetWidth, h = tip.offsetHeight;
    tip.style.left = Math.min(window.innerWidth - w - 8, Math.max(8, x + 14)) + "px";
    tip.style.top = Math.max(8, y - h - 12) + "px";
    tip.classList.add("on");
  }
  function hide() { if (tip) tip.classList.remove("on"); }
  document.addEventListener("mousemove", function (e) {
    var el = e.target.closest && e.target.closest("[data-tip]");
    if (el) show(el, e.clientX, e.clientY); else hide();
  });
  document.addEventListener("focusin", function (e) {
    var el = e.target.closest && e.target.closest("[data-tip]");
    if (!el) return;
    var r = el.getBoundingClientRect();
    show(el, r.left + r.width / 2, r.top);
  });
  document.addEventListener("focusout", hide);
})();
document.addEventListener("submit", function (e) {
  var msg = e.submitter && e.submitter.getAttribute("data-confirm");
  if (msg && !window.confirm(msg)) e.preventDefault();
});
