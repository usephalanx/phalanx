<?php
/**
 * Template part for displaying posts.
 *
 * @package Teamworks
 * @since   1.0.0
 */

if (! defined('ABSPATH')) {
    exit;
}
?>

<article id="post-<?php the_ID(); ?>" <?php post_class(); ?>>
    <header class="entry-header">
        <?php
        if (is_singular()) :
            the_title('<h1 class="entry-title">', '</h1>');
        else :
            the_title(
                '<h2 class="entry-title"><a href="' . esc_url(get_permalink()) . '" rel="bookmark">',
                '</a></h2>'
            );
        endif;

        if ('post' === get_post_type()) :
        ?>
            <div class="entry-meta">
                <?php
                teamworks_posted_on();
                teamworks_posted_by();
                ?>
            </div>
        <?php endif; ?>
    </header>

    <?php teamworks_post_thumbnail(); ?>

    <div class="entry-content">
        <?php
        if (is_singular()) :
            the_content(
                sprintf(
                    wp_kses(
                        /* translators: %s: post title */
                        __('Continue reading<span class="screen-reader-text"> "%s"</span>', 'teamworks'),
                        ['span' => ['class' => []]]
                    ),
                    wp_kses_post(get_the_title())
                )
            );

            wp_link_pages([
                'before' => '<div class="page-links">' . esc_html__('Pages:', 'teamworks'),
                'after'  => '</div>',
            ]);
        else :
            the_excerpt();
        endif;
        ?>
    </div>

    <footer class="entry-footer">
        <?php teamworks_entry_footer(); ?>
    </footer>
</article><!-- #post-<?php the_ID(); ?> -->
