/**
 * Teamworks theme — Main JavaScript.
 *
 * Handles mobile navigation toggle and other front-end interactions.
 *
 * @package Teamworks
 * @since   1.0.0
 */

(function () {
    'use strict';

    /**
     * Initialize mobile navigation toggle.
     */
    function initMobileNav() {
        var toggle = document.querySelector('.menu-toggle');
        var nav = document.querySelector('.main-navigation');

        if (!toggle || !nav) {
            return;
        }

        toggle.addEventListener('click', function () {
            nav.classList.toggle('toggled');
            var expanded = toggle.getAttribute('aria-expanded') === 'true';
            toggle.setAttribute('aria-expanded', String(!expanded));
        });

        // Close menu when clicking outside.
        document.addEventListener('click', function (event) {
            if (
                nav.classList.contains('toggled') &&
                !nav.contains(event.target)
            ) {
                nav.classList.remove('toggled');
                toggle.setAttribute('aria-expanded', 'false');
            }
        });

        // Close menu on Escape key.
        document.addEventListener('keydown', function (event) {
            if (event.key === 'Escape' && nav.classList.contains('toggled')) {
                nav.classList.remove('toggled');
                toggle.setAttribute('aria-expanded', 'false');
                toggle.focus();
            }
        });
    }

    /**
     * Smooth scroll for anchor links.
     */
    function initSmoothScroll() {
        var links = document.querySelectorAll('a[href^="#"]');

        links.forEach(function (link) {
            link.addEventListener('click', function (event) {
                var targetId = this.getAttribute('href');
                if (targetId === '#') {
                    return;
                }

                var target = document.querySelector(targetId);
                if (target) {
                    event.preventDefault();
                    target.scrollIntoView({ behavior: 'smooth', block: 'start' });
                }
            });
        });
    }

    /**
     * Add scroll-based header shadow.
     */
    function initHeaderScroll() {
        var header = document.querySelector('.site-header');
        if (!header) {
            return;
        }

        window.addEventListener(
            'scroll',
            function () {
                if (window.scrollY > 10) {
                    header.classList.add('scrolled');
                } else {
                    header.classList.remove('scrolled');
                }
            },
            { passive: true }
        );
    }

    // Initialize all modules when DOM is ready.
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function () {
            initMobileNav();
            initSmoothScroll();
            initHeaderScroll();
        });
    } else {
        initMobileNav();
        initSmoothScroll();
        initHeaderScroll();
    }
})();
