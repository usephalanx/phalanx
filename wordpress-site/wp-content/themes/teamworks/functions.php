<?php
/**
 * Teamworks theme functions and definitions.
 *
 * Sets up theme defaults, registers support for various WordPress features,
 * and loads required theme files.
 *
 * @package Teamworks
 * @since   1.0.0
 */

if (! defined('ABSPATH')) {
    exit;
}

/**
 * Theme version constant.
 */
define('TEAMWORKS_VERSION', '1.0.0');

/**
 * Content width in pixels, based on the theme layout.
 *
 * @global int $content_width
 */
if (! isset($content_width)) {
    $content_width = 800;
}

/**
 * Sets up theme defaults and registers support for various WordPress features.
 *
 * @return void
 */
function teamworks_setup(): void {
    // Make the theme available for translation.
    load_theme_textdomain('teamworks', get_template_directory() . '/languages');

    // Add default posts and comments RSS feed links to head.
    add_theme_support('automatic-feed-links');

    // Let WordPress manage the document title.
    add_theme_support('title-tag');

    // Enable support for Post Thumbnails on posts and pages.
    add_theme_support('post-thumbnails');
    set_post_thumbnail_size(1200, 630, true);

    // Add custom image sizes.
    add_image_size('teamworks-card', 600, 400, true);
    add_image_size('teamworks-hero', 1920, 800, true);

    // Register navigation menus.
    register_nav_menus([
        'primary'  => esc_html__('Primary Menu', 'teamworks'),
        'footer'   => esc_html__('Footer Menu', 'teamworks'),
    ]);

    // Switch default core markup to valid HTML5.
    add_theme_support('html5', [
        'search-form',
        'comment-form',
        'comment-list',
        'gallery',
        'caption',
        'style',
        'script',
        'navigation-widgets',
    ]);

    // Custom background support.
    add_theme_support('custom-background', [
        'default-color' => 'ffffff',
    ]);

    // Custom logo support.
    add_theme_support('custom-logo', [
        'height'      => 80,
        'width'       => 250,
        'flex-height' => true,
        'flex-width'  => true,
    ]);

    // Block editor support.
    add_theme_support('wp-block-styles');
    add_theme_support('align-wide');
    add_theme_support('responsive-embeds');

    // Editor color palette matching theme variables.
    add_theme_support('editor-color-palette', [
        [
            'name'  => esc_html__('Primary', 'teamworks'),
            'slug'  => 'primary',
            'color' => '#1a1a2e',
        ],
        [
            'name'  => esc_html__('Secondary', 'teamworks'),
            'slug'  => 'secondary',
            'color' => '#16213e',
        ],
        [
            'name'  => esc_html__('Accent', 'teamworks'),
            'slug'  => 'accent',
            'color' => '#0f3460',
        ],
        [
            'name'  => esc_html__('Highlight', 'teamworks'),
            'slug'  => 'highlight',
            'color' => '#e94560',
        ],
    ]);
}
add_action('after_setup_theme', 'teamworks_setup');

/**
 * Register widget areas.
 *
 * @return void
 */
function teamworks_widgets_init(): void {
    register_sidebar([
        'name'          => esc_html__('Sidebar', 'teamworks'),
        'id'            => 'sidebar-1',
        'description'   => esc_html__('Add widgets here to appear in the sidebar.', 'teamworks'),
        'before_widget' => '<section id="%1$s" class="widget %2$s">',
        'after_widget'  => '</section>',
        'before_title'  => '<h2 class="widget-title">',
        'after_title'   => '</h2>',
    ]);

    register_sidebar([
        'name'          => esc_html__('Footer Widgets', 'teamworks'),
        'id'            => 'footer-1',
        'description'   => esc_html__('Add widgets here to appear in the footer.', 'teamworks'),
        'before_widget' => '<section id="%1$s" class="widget %2$s">',
        'after_widget'  => '</section>',
        'before_title'  => '<h2 class="widget-title">',
        'after_title'   => '</h2>',
    ]);
}
add_action('widgets_init', 'teamworks_widgets_init');

// ── Include modular theme files ──────────────────────────────────

require get_template_directory() . '/inc/enqueue.php';
require get_template_directory() . '/inc/template-tags.php';
require get_template_directory() . '/inc/customizer.php';
