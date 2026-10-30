"use strict";
/* CivicTrace case workbench: local file-input helper.
 *
 * An HTML file input can only transmit a *filename* unless the form is multipart/form-data, and
 * this build deliberately adds no multipart dependency. This helper reads the picked .txt file in
 * the page, keeps its exact bytes, and writes them base64-encoded into the hidden fields the server
 * already accepts (the same shape as POST /api/cases). Nothing is fetched, uploaded or sent
 * anywhere except back to this same origin when the form is submitted.
 *
 * If this script does not run, the server refuses the request with the typed code
 * UPLOAD_NEEDS_SCRIPT instead of importing an empty source or a bare filename.
 */
(function () {
  var MAX_BYTES = 65536;

  function setStatus(node, text) {
    if (node) { node.textContent = text; }
  }

  Array.prototype.forEach.call(
    document.querySelectorAll('form[data-upload-form]'),
    function (form) {
      var picker = form.querySelector('input[type="file"]');
      var encoded = form.querySelector('input[name="text_base64"]');
      var filename = form.querySelector('input[name="filename"]');
      var status = form.querySelector('[data-upload-status]');
      if (!picker || !encoded || !filename) { return; }

      picker.addEventListener('change', function () {
        var file = picker.files && picker.files[0];
        encoded.value = '';
        filename.value = '';
        if (!file) { setStatus(status, ''); return; }
        if (!/\.txt$/i.test(file.name)) {
          picker.value = '';
          setStatus(status, 'Refused locally: pick a .txt file. Other document types are not imported.');
          return;
        }
        if (file.size > MAX_BYTES) {
          picker.value = '';
          setStatus(status, 'Refused locally: the file is larger than ' + MAX_BYTES + ' bytes.');
          return;
        }
        var reader = new FileReader();
        reader.onload = function () {
          var bytes = new Uint8Array(reader.result);
          var binary = '';
          for (var i = 0; i < bytes.length; i += 1) {
            binary += String.fromCharCode(bytes[i]);
          }
          encoded.value = window.btoa(binary);
          filename.value = file.name;
          setStatus(status, 'Selected ' + file.name + ' (' + bytes.length + ' bytes). '
            + 'The bytes stay in this page until you submit.');
        };
        reader.onerror = function () {
          picker.value = '';
          encoded.value = '';
          filename.value = '';
          setStatus(status, 'The file could not be read locally; nothing was imported.');
        };
        reader.readAsArrayBuffer(file);
      });

      form.addEventListener('submit', function (event) {
        if (picker.files && picker.files.length && !encoded.value) {
          event.preventDefault();
          setStatus(status, 'Wait for the file to be read, or paste the text instead.');
        }
      });
    }
  );
}());
