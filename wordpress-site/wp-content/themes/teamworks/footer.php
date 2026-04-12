<?php
/**
 * The footer template.
 *
 * Contains the closing of the site-content div and all content after.
 *
 * @package Teamworks
 * @since   1.0.0
 */

if (! defined('ABSPATH')) {
    exit;
}
?>
    </div><!-- #content .site-content -->

    <footer id="colophon" class="site-footer" role="contentinfo">
        <?php if (is_active_sidebar('footer-1')) : ?>
            <div class="footer-widgets">
                <?php dynamic_sidebar('footer-1'); ?>
            </div>
        <?php endif; ?>

        <?php if (has_nav_menu('footer')) : ?>
            <nav class="footer-navigation" aria-label="<?php esc_attr_e('Footer Menu', 'teamworks'); ?>">
                <?php
                wp_nav_menu([
                    'theme_location' => 'footer',
                    'menu_id'        => 'footer-menu',
                    'container'      => false,
                    'depth'          => 1,
                ]);
                ?>
            </nav>
        <?php endif; ?>

        <div class="site-info">
            <?php teamworks_render_footer_copyright(); ?>
        </div><!-- .site-info -->

        <?php
        $social_links = [
            'twitter'  => get_theme_mod('teamworks_social_twitter'),
            'github'   => get_theme_mod('teamworks_social_github'),
            'linkedin' => get_theme_mod('teamworks_social_linkedin'),
        ];
        $has_social = array_filter($social_links);
        if (! empty($has_social)) :
        ?>
            <div class="social-links">
                <?php foreach ($has_social as $network => $url) : ?>
                    <a href="<?php echo esc_url($url); ?>"
                       target="_blank"
                       rel="noopener noreferrer"
                       aria-label="<?php echo esc_attr(ucfirst($network)); ?>">
                        <?php echo esc_html(ucfirst($network)); ?>
                    </a>
                <?php endforeach; ?>
            </div>
        <?php endif; ?>
    </footer><!-- #colophon -->

</div><!-- #page .site -->

<?php wp_footer(); ?>
</body>
</html>
