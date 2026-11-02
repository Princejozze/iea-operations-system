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


/* htmx: a refused edit answers 422 with the message to show. Swap it in (htmx drops 4xx by default). */
document.addEventListener("htmx:beforeSwap", function (event) {
  if (event.detail.xhr && event.detail.xhr.status === 422) {
    event.detail.shouldSwap = true;
    event.detail.isError = false;
  }
});

/* A form with data-confirm asks first (a native dialog: no framework, works on every phone). */
document.addEventListener("submit", function (event) {
  var text = event.target.getAttribute && event.target.getAttribute("data-confirm");
  if (text && !window.confirm(text)) event.preventDefault();
});


/* The stage strip scrolls sideways on a phone; start with the current stage in view, not the first. */
document.addEventListener("DOMContentLoaded", function () {
  var current = document.querySelector(".stages li.current");
  if (current && current.parentElement.scrollWidth > current.parentElement.clientWidth) {
    current.parentElement.scrollLeft = current.offsetLeft - current.parentElement.clientWidth / 2 + current.offsetWidth / 2;
  }
});
