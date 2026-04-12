<?php
/**
 * Template part for displaying a message when no posts are found.
 *
 * @package Teamworks
 * @since   1.0.0
 */

if (! defined('ABSPATH')) {
    exit;
}
?>

<section class="no-results not-found">
    <header class="page-header">
        <h1 class="page-title">
            <?php esc_html_e('Nothing Found', 'teamworks'); ?>
        </h1>
    </header>

    <div class="page-content">
        <?php if (is_home() && current_user_can('publish_posts')) : ?>

            <p>
                <?php
                printf(
                    wp_kses(
                        /* translators: %s: publish post URL */
                        __('Ready to publish your first post? <a href="%s">Get started here</a>.', 'teamworks'),
                        ['a' => ['href' => []]]
                    ),
                    esc_url(admin_url('post-new.php'))
                );
                ?>
            </p>

        <?php elseif (is_search()) : ?>

            <p><?php esc_html_e('Sorry, nothing matched your search terms. Please try again with different keywords.', 'teamworks'); ?></p>
            <?php get_search_form(); ?>

        <?php else : ?>

            <p><?php esc_html_e('It seems we can&rsquo;t find what you&rsquo;re looking for. Try searching.', 'teamworks'); ?></p>
            <?php get_search_form(); ?>

        <?php endif; ?>
    </div>
</section>
