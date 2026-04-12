<?php
/**
 * Template part for displaying search results.
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
        <?php the_title(sprintf('<h2 class="entry-title"><a href="%s" rel="bookmark">', esc_url(get_permalink())), '</a></h2>'); ?>

        <?php if ('post' === get_post_type()) : ?>
            <div class="entry-meta">
                <?php
                teamworks_posted_on();
                teamworks_posted_by();
                ?>
            </div>
        <?php endif; ?>
    </header>

    <?php teamworks_post_thumbnail('teamworks-card'); ?>

    <div class="entry-summary">
        <?php the_excerpt(); ?>
    </div>

    <footer class="entry-footer">
        <?php teamworks_entry_footer(); ?>
    </footer>
</article><!-- #post-<?php the_ID(); ?> -->
