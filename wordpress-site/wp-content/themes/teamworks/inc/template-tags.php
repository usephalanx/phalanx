<?php
/**
 * Custom template tags for the Teamworks theme.
 *
 * @package Teamworks
 * @since   1.0.0
 */

if (! defined('ABSPATH')) {
    exit;
}

/**
 * Print HTML with meta information for the current post date/time.
 *
 * @return void
 */
function teamworks_posted_on(): void {
    $time_string = '<time class="entry-date published updated" datetime="%1$s">%2$s</time>';

    if (get_the_time('U') !== get_the_modified_time('U')) {
        $time_string = '<time class="entry-date published" datetime="%1$s">%2$s</time>';
        $time_string .= '<time class="updated screen-reader-text" datetime="%3$s">%4$s</time>';
    }

    $time_string = sprintf(
        $time_string,
        esc_attr(get_the_date(DATE_W3C)),
        esc_html(get_the_date()),
        esc_attr(get_the_modified_date(DATE_W3C)),
        esc_html(get_the_modified_date())
    );

    printf(
        '<span class="posted-on">%s %s</span>',
        esc_html__('Published', 'teamworks'),
        $time_string
    );
}

/**
 * Print HTML with meta information for the current author.
 *
 * @return void
 */
function teamworks_posted_by(): void {
    printf(
        '<span class="byline"> %s <span class="author vcard"><a class="url fn n" href="%s">%s</a></span></span>',
        esc_html__('by', 'teamworks'),
        esc_url(get_author_posts_url(get_the_author_meta('ID'))),
        esc_html(get_the_author())
    );
}

/**
 * Print HTML with meta information for the categories, tags, and comments.
 *
 * @return void
 */
function teamworks_entry_footer(): void {
    if ('post' === get_post_type()) {
        $categories_list = get_the_category_list(esc_html__(', ', 'teamworks'));
        if ($categories_list) {
            printf(
                '<span class="cat-links">%s %s</span>',
                esc_html__('Posted in', 'teamworks'),
                $categories_list
            );
        }

        $tags_list = get_the_tag_list('', esc_html__(', ', 'teamworks'));
        if ($tags_list) {
            printf(
                '<span class="tags-links">%s %s</span>',
                esc_html__('Tagged', 'teamworks'),
                $tags_list
            );
        }
    }

    if (! is_single() && ! post_password_required() && (comments_open() || get_comments_number())) {
        echo '<span class="comments-link">';
        comments_popup_link(
            sprintf(
                wp_kses(
                    /* translators: %s: post title */
                    __('Leave a Comment<span class="screen-reader-text"> on %s</span>', 'teamworks'),
                    ['span' => ['class' => []]]
                ),
                wp_kses_post(get_the_title())
            )
        );
        echo '</span>';
    }

    edit_post_link(
        sprintf(
            wp_kses(
                /* translators: %s: post title */
                __('Edit <span class="screen-reader-text">%s</span>', 'teamworks'),
                ['span' => ['class' => []]]
            ),
            wp_kses_post(get_the_title())
        ),
        '<span class="edit-link">',
        '</span>'
    );
}

/**
 * Display post thumbnail with fallback.
 *
 * @param string $size Image size name. Default 'post-thumbnail'.
 * @return void
 */
function teamworks_post_thumbnail(string $size = 'post-thumbnail'): void {
    if (post_password_required() || is_attachment() || ! has_post_thumbnail()) {
        return;
    }

    if (is_singular()) {
        printf('<div class="post-thumbnail">');
        the_post_thumbnail($size);
        printf('</div>');
    } else {
        printf('<div class="post-thumbnail">');
        printf(
            '<a class="post-thumbnail-link" href="%s" aria-hidden="true" tabindex="-1">',
            esc_url(get_permalink())
        );
        the_post_thumbnail($size, ['alt' => the_title_attribute(['echo' => false])]);
        printf('</a></div>');
    }
}

/**
 * Display pagination for archive pages.
 *
 * @return void
 */
function teamworks_pagination(): void {
    the_posts_pagination([
        'mid_size'  => 2,
        'prev_text' => sprintf(
            '<span class="screen-reader-text">%s</span> &laquo;',
            esc_html__('Previous', 'teamworks')
        ),
        'next_text' => sprintf(
            '&raquo; <span class="screen-reader-text">%s</span>',
            esc_html__('Next', 'teamworks')
        ),
    ]);
}
