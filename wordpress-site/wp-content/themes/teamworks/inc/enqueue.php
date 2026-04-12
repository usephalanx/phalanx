<?php
/**
 * Enqueue scripts and styles.
 *
 * @package Teamworks
 * @since   1.0.0
 */

if (! defined('ABSPATH')) {
    exit;
}

/**
 * Enqueue front-end styles and scripts.
 *
 * @return void
 */
function teamworks_scripts(): void {
    // Main theme stylesheet (style.css at theme root).
    wp_enqueue_style(
        'teamworks-style',
        get_stylesheet_uri(),
        [],
        TEAMWORKS_VERSION
    );

    // Additional custom CSS.
    wp_enqueue_style(
        'teamworks-main',
        get_template_directory_uri() . '/assets/css/main.css',
        ['teamworks-style'],
        TEAMWORKS_VERSION
    );

    // Main JavaScript.
    wp_enqueue_script(
        'teamworks-main',
        get_template_directory_uri() . '/assets/js/main.js',
        [],
        TEAMWORKS_VERSION,
        true
    );

    // Pass data to JS.
    wp_localize_script('teamworks-main', 'teamworksData', [
        'ajaxUrl' => admin_url('admin-ajax.php'),
        'nonce'   => wp_create_nonce('teamworks_nonce'),
        'siteUrl' => home_url('/'),
    ]);

    // Threaded comments reply script.
    if (is_singular() && comments_open() && get_option('thread_comments')) {
        wp_enqueue_script('comment-reply');
    }
}
add_action('wp_enqueue_scripts', 'teamworks_scripts');

/**
 * Enqueue block editor styles.
 *
 * @return void
 */
function teamworks_editor_styles(): void {
    add_editor_style('assets/css/editor-style.css');
}
add_action('after_setup_theme', 'teamworks_editor_styles');

/**
 * Add preconnect for Google Fonts (if used in the future).
 *
 * @param array<int, array<string, string>> $urls          URLs to print for resource hints.
 * @param string                            $relation_type The relation type the URLs are printed for.
 * @return array<int, array<string, string>>
 */
function teamworks_resource_hints(array $urls, string $relation_type): array {
    if ('preconnect' === $relation_type) {
        $urls[] = [
            'href' => 'https://fonts.googleapis.com',
            'crossorigin' => 'anonymous',
        ];
        $urls[] = [
            'href' => 'https://fonts.gstatic.com',
            'crossorigin' => 'anonymous',
        ];
    }
    return $urls;
}
add_filter('wp_resource_hints', 'teamworks_resource_hints', 10, 2);
