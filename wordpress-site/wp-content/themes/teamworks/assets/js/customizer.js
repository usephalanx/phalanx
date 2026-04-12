/**
 * Teamworks theme — Customizer live preview.
 *
 * Updates the preview in real time as Customizer settings change.
 *
 * @package Teamworks
 * @since   1.0.0
 */

(function (api) {
    'use strict';

    // Site title.
    api('blogname', function (value) {
        value.bind(function (to) {
            document.querySelector('.site-title a').textContent = to;
        });
    });

    // Site description.
    api('blogdescription', function (value) {
        value.bind(function (to) {
            var el = document.querySelector('.site-description');
            if (el) {
                el.textContent = to;
                el.style.display = to ? '' : 'none';
            }
        });
    });

    // Footer copyright.
    api('teamworks_footer_copyright', function (value) {
        value.bind(function (to) {
            var el = document.querySelector('.site-info');
            if (el) {
                el.innerHTML = to;
            }
        });
    });
})(wp.customize);
