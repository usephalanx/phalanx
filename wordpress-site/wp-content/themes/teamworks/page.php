<?php
/**
 * The template for displaying all pages.
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
        get_template_part('template-parts/content', 'page');

        // If comments are open or there is at least one comment, load the template.
        if (comments_open() || get_comments_number()) :
            comments_template();
        endif;

    endwhile;
    ?>

</main><!-- #primary -->

<?php
get_footer();
