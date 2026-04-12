<?php
/**
 * The template for displaying 404 pages (not found).
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

    <section class="error-404 not-found">
        <header class="page-header">
            <h1 class="page-title">
                <?php esc_html_e('Page Not Found', 'teamworks'); ?>
            </h1>
        </header>

        <div class="page-content">
            <p><?php esc_html_e('It looks like nothing was found at this location. Try searching for what you need.', 'teamworks'); ?></p>

            <?php get_search_form(); ?>

            <div class="widget">
                <h2 class="widget-title"><?php esc_html_e('Recent Posts', 'teamworks'); ?></h2>
                <ul>
                    <?php
                    wp_get_archives([
                        'type'  => 'postbypost',
                        'limit' => 10,
                    ]);
                    ?>
                </ul>
            </div>
        </div>
    </section>

</main><!-- #primary -->

<?php
get_footer();
