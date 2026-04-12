<?php
/**
 * The header template.
 *
 * Displays the <head> section and everything up to <div class="site-content">.
 *
 * @package Teamworks
 * @since   1.0.0
 */

if (! defined('ABSPATH')) {
    exit;
}
?>
<!doctype html>
<html <?php language_attributes(); ?>>
<head>
    <meta charset="<?php bloginfo('charset'); ?>">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link rel="profile" href="https://gmpg.org/xfn/11">
    <?php wp_head(); ?>
</head>

<body <?php body_class(); ?>>
<?php wp_body_open(); ?>

<div id="page" class="site">
    <a class="skip-link screen-reader-text" href="#primary">
        <?php esc_html_e('Skip to content', 'teamworks'); ?>
    </a>

    <header id="masthead" class="site-header" role="banner">
        <div class="site-branding">
            <?php if (has_custom_logo()) : ?>
                <div class="site-logo"><?php the_custom_logo(); ?></div>
            <?php endif; ?>

            <div class="site-branding-text">
                <?php if (is_front_page() && is_home()) : ?>
                    <h1 class="site-title">
                        <a href="<?php echo esc_url(home_url('/')); ?>" rel="home">
                            <?php bloginfo('name'); ?>
                        </a>
                    </h1>
                <?php else : ?>
                    <p class="site-title">
                        <a href="<?php echo esc_url(home_url('/')); ?>" rel="home">
                            <?php bloginfo('name'); ?>
                        </a>
                    </p>
                <?php endif; ?>

                <?php
                $teamworks_description = get_bloginfo('description', 'display');
                if ($teamworks_description || is_customize_preview()) :
                ?>
                    <p class="site-description"><?php echo esc_html($teamworks_description); ?></p>
                <?php endif; ?>
            </div>
        </div><!-- .site-branding -->
    </header><!-- #masthead -->

    <?php if (has_nav_menu('primary')) : ?>
        <nav id="site-navigation" class="main-navigation" role="navigation"
             aria-label="<?php esc_attr_e('Primary Menu', 'teamworks'); ?>">
            <button class="menu-toggle" aria-controls="primary-menu" aria-expanded="false">
                <?php esc_html_e('Menu', 'teamworks'); ?>
            </button>
            <?php
            wp_nav_menu([
                'theme_location' => 'primary',
                'menu_id'        => 'primary-menu',
                'container'      => false,
            ]);
            ?>
        </nav><!-- #site-navigation -->
    <?php endif; ?>

    <div id="content" class="site-content">
