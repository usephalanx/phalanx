<?php
/**
 * The main template file.
 *
 * The most generic template file in a WordPress theme. Used when no more
 * specific template matches a query. Displays a list of posts.
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

    <?php if (have_posts()) : ?>

        <?php if (is_home() && ! is_front_page()) : ?>
            <header class="page-header">
                <h1 class="page-title">
                    <?php single_post_title(); ?>
                </h1>
            </header>
        <?php endif; ?>

        <?php
        while (have_posts()) :
            the_post();
            get_template_part('template-parts/content', get_post_type());
        endwhile;

        teamworks_pagination();

    else :

        get_template_part('template-parts/content', 'none');

    endif;
    ?>

</main><!-- #primary -->

<?php
get_sidebar();
get_footer();
