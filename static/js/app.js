/* One-click copy for tender references (R18). Plain JS, no framework.
   Any element with data-copy="text" copies that text when clicked. */
document.addEventListener("click", function (event) {
  var button = event.target.closest("[data-copy]");
  if (!button) return;
  var text = button.getAttribute("data-copy");
  var done = function () {
    var label = button.textContent;
    button.textContent = "Copied";
    setTimeout(function () { button.textContent = label; }, 1500);
  };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(done);
  } else {
    var area = document.createElement("textarea");
    area.value = text;
    document.body.appendChild(area);
    area.select();
    try { document.execCommand("copy"); done(); } finally { document.body.removeChild(area); }
  }
});
