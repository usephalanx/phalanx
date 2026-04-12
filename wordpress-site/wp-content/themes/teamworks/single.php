<?php
/**
 * The template for displaying all single posts.
 *
 * @package Teamworks
 * @since   1.0.0
 */

if (! defined('ABSPATH')) {
    exit;
}

get_header();
?>

<main id="primary" class="content-area" role="main">

    <?php
    while (have_posts()) :
        the_post();
        get_template_part('template-parts/content', get_post_type());

        // Post navigation.
        the_post_navigation([
            'prev_text' => '<span class="nav-subtitle">' . esc_html__('Previous:', 'teamworks') . '</span> <span class="nav-title">%title</span>',
            'next_text' => '<span class="nav-subtitle">' . esc_html__('Next:', 'teamworks') . '</span> <span class="nav-title">%title</span>',
        ]);

        // If comments are open or there is at least one comment, load the template.
        if (comments_open() || get_comments_number()) :
            comments_template();
        endif;

    endwhile;
    ?>

</main><!-- #primary -->

<?php
get_sidebar();
get_footer();
