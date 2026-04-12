#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────
# setup-wordpress.sh — Automated WordPress setup via WP-CLI.
#
# Runs inside the wp-cli container to install WordPress core,
# activate the Teamworks theme, and create initial pages.
#
# Usage:
#   docker compose run --rm wp-cli /scripts/setup-wordpress.sh
# ──────────────────────────────────────────────────────────────────

set -euo pipefail

SITE_URL="${WORDPRESS_SITE_URL:-http://localhost:8080}"
SITE_TITLE="${WORDPRESS_SITE_TITLE:-Teamworks}"
ADMIN_USER="${WORDPRESS_ADMIN_USER:-admin}"
ADMIN_PASS="${WORDPRESS_ADMIN_PASSWORD:-admin_dev_password}"
ADMIN_EMAIL="${WORDPRESS_ADMIN_EMAIL:-admin@example.com}"

echo "==> Waiting for database..."
sleep 5

echo "==> Installing WordPress core..."
wp core install \
    --url="${SITE_URL}" \
    --title="${SITE_TITLE}" \
    --admin_user="${ADMIN_USER}" \
    --admin_password="${ADMIN_PASS}" \
    --admin_email="${ADMIN_EMAIL}" \
    --skip-email \
    --allow-root

echo "==> Activating Teamworks theme..."
wp theme activate teamworks --allow-root

echo "==> Configuring permalink structure..."
wp rewrite structure '/%postname%/' --allow-root
wp rewrite flush --allow-root

echo "==> Creating default pages..."
wp post create --post_type=page --post_title='Home' --post_status=publish --allow-root
wp post create --post_type=page --post_title='About' --post_status=publish --allow-root
wp post create --post_type=page --post_title='Contact' --post_status=publish --allow-root
wp post create --post_type=page --post_title='Blog' --post_status=publish --allow-root

echo "==> Setting front page..."
HOME_ID=$(wp post list --post_type=page --name=home --field=ID --allow-root)
BLOG_ID=$(wp post list --post_type=page --name=blog --field=ID --allow-root)
wp option update show_on_front 'page' --allow-root
wp option update page_on_front "${HOME_ID}" --allow-root
wp option update page_for_posts "${BLOG_ID}" --allow-root

echo "==> Creating primary menu..."
wp menu create "Primary Menu" --allow-root
wp menu item add-post "Primary Menu" "${HOME_ID}" --title="Home" --allow-root

ABOUT_ID=$(wp post list --post_type=page --name=about --field=ID --allow-root)
wp menu item add-post "Primary Menu" "${ABOUT_ID}" --title="About" --allow-root

CONTACT_ID=$(wp post list --post_type=page --name=contact --field=ID --allow-root)
wp menu item add-post "Primary Menu" "${CONTACT_ID}" --title="Contact" --allow-root

wp menu item add-post "Primary Menu" "${BLOG_ID}" --title="Blog" --allow-root
wp menu location assign "Primary Menu" primary --allow-root

echo "==> Removing default content..."
wp post delete 1 --force --allow-root 2>/dev/null || true   # "Hello world!" post
wp post delete 2 --force --allow-root 2>/dev/null || true   # Sample page
wp comment delete 1 --force --allow-root 2>/dev/null || true # Default comment

echo "==> Configuring WordPress settings..."
wp option update blogdescription "AI-powered software development" --allow-root
wp option update timezone_string "America/Los_Angeles" --allow-root
wp option update date_format "F j, Y" --allow-root
wp option update time_format "g:i a" --allow-root
wp option update posts_per_page 10 --allow-root

echo "==> Disabling unnecessary default plugins..."
wp plugin deactivate --all --allow-root 2>/dev/null || true

echo ""
echo "========================================="
echo "  WordPress setup complete!"
echo "  URL:   ${SITE_URL}"
echo "  Admin: ${SITE_URL}/wp-admin"
echo "  User:  ${ADMIN_USER}"
echo "  Pass:  ${ADMIN_PASS}"
echo "========================================="
