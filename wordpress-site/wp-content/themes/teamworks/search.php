<?php
/**
 * The template for displaying search results pages.
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

        <header class="page-header">
            <h1 class="page-title">
                <?php
                printf(
                    /* translators: %s: search query */
                    esc_html__('Search Results for: %s', 'teamworks'),
                    '<span>' . get_search_query() . '</span>'
                );
                ?>
            </h1>
        </header>

        <?php
        while (have_posts()) :
            the_post();
            get_template_part('template-parts/content', 'search');
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
