<?php
/**
 * Theme Customizer settings.
 *
 * @package Teamworks
 * @since   1.0.0
 */

if (! defined('ABSPATH')) {
    exit;
}

/**
 * Register Customizer settings, sections, and controls.
 *
 * @param WP_Customize_Manager $wp_customize Theme Customizer object.
 * @return void
 */
function teamworks_customize_register(WP_Customize_Manager $wp_customize): void {
    // ── Section: Footer ──────────────────────────────────────────
    $wp_customize->add_section('teamworks_footer', [
        'title'    => esc_html__('Footer Settings', 'teamworks'),
        'priority' => 120,
    ]);

    // Footer copyright text.
    $wp_customize->add_setting('teamworks_footer_copyright', [
        'default'           => sprintf(
            /* translators: %s: current year */
            esc_html__('© %s Teamworks. All rights reserved.', 'teamworks'),
            date('Y')
        ),
        'sanitize_callback' => 'wp_kses_post',
        'transport'         => 'postMessage',
    ]);

    $wp_customize->add_control('teamworks_footer_copyright', [
        'label'   => esc_html__('Copyright Text', 'teamworks'),
        'section' => 'teamworks_footer',
        'type'    => 'textarea',
    ]);

    // ── Section: Social Links ────────────────────────────────────
    $wp_customize->add_section('teamworks_social', [
        'title'    => esc_html__('Social Links', 'teamworks'),
        'priority' => 130,
    ]);

    $social_networks = [
        'twitter'  => esc_html__('Twitter / X URL', 'teamworks'),
        'github'   => esc_html__('GitHub URL', 'teamworks'),
        'linkedin' => esc_html__('LinkedIn URL', 'teamworks'),
    ];

    foreach ($social_networks as $network => $label) {
        $setting_id = "teamworks_social_{$network}";

        $wp_customize->add_setting($setting_id, [
            'default'           => '',
            'sanitize_callback' => 'esc_url_raw',
            'transport'         => 'postMessage',
        ]);

        $wp_customize->add_control($setting_id, [
            'label'   => $label,
            'section' => 'teamworks_social',
            'type'    => 'url',
        ]);
    }

    // Selective refresh for footer copyright.
    if (isset($wp_customize->selective_refresh)) {
        $wp_customize->selective_refresh->add_partial('teamworks_footer_copyright', [
            'selector'        => '.site-info',
            'render_callback' => 'teamworks_render_footer_copyright',
        ]);
    }
}
add_action('customize_register', 'teamworks_customize_register');

/**
 * Render the footer copyright text (used for selective refresh).
 *
 * @return void
 */
function teamworks_render_footer_copyright(): void {
    echo wp_kses_post(get_theme_mod(
        'teamworks_footer_copyright',
        sprintf('&copy; %s Teamworks. All rights reserved.', date('Y'))
    ));
}

/**
 * Enqueue Customizer preview JavaScript.
 *
 * @return void
 */
function teamworks_customize_preview_js(): void {
    wp_enqueue_script(
        'teamworks-customizer',
        get_template_directory_uri() . '/assets/js/customizer.js',
        ['customize-preview'],
        TEAMWORKS_VERSION,
        true
    );
}
add_action('customize_preview_init', 'teamworks_customize_preview_js');
