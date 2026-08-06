# FetchSandbox Product Brief for Phalanx

Last updated: 2026-05-03

## Purpose

This brief gives Phalanx enough context to reason about FetchSandbox product, GTM, positioning, and execution tasks without needing to rediscover the product every run.

Use it when Raj asks Phalanx to build, write, audit, or plan anything related to FetchSandbox.

## Product

FetchSandbox is a developer tool at `fetchsandbox.com`.

It turns an OpenAPI spec into a live developer portal and stateful API sandbox. Developers can test real API workflows without production credentials, account setup, or hand-written mock servers.

Current shipped position:

- Stateful sandboxes generated from OpenAPI specs
- Real resource state across requests
- Curated workflows with ID chaining
- Webhook events on state changes
- Seed data for realistic list/read flows
- API docs portal per spec
- Browser playground and CLI
- 46 production specs reachable via `/api/portals`
- 280 workflows in the catalog
- Polar onboarded on 2026-05-03 as the first new spec under the Day 14 gate

Core thesis:

> API integrations fail in the lifecycle, not the endpoint.

## ICP

Primary users:

- API developers building third-party integrations
- Founders wiring billing, email, auth, messaging, and payments
- Solutions engineers who need API demos
- Teams that need partner sandboxes before production access
- Developers using coding agents who need executable API context

Pain:

- Vendor sandboxes require signup, keys, org setup, dashboards, and sometimes verification
- Static mocks return example responses but do not preserve state
- Docs show endpoints, but real integrations need lifecycle behavior
- Webhook handlers break on retries, duplicates, ordering, signatures, and missing events
- Staging environments drift, go down, or contain unsafe production-like data

## Positioning

Short version:

> Stop writing mock servers. Turn any OpenAPI spec into a stateful sandbox.

More technical version:

> FetchSandbox proves the workflow, not just the request. It lets a developer create a resource, read it back, move it through lifecycle states, and inspect emitted webhook events before production.

Do not over-position as a full vendor sandbox replacement. The honest positioning:

- Use FetchSandbox early to validate integration shape, state, ID chaining, and webhook flow.
- Use the vendor's real sandbox later for final account-specific/payment-specific validation.

## Moat

The Reddit post that triggered this brief was generic, but one useful insight applies: developer tools win when code and workflow expertise become the moat.

FetchSandbox's moat is not just "mock API responses." The moat is systems knowledge encoded into workflows:

- OpenAPI parsing
- State inference
- Resource relationship handling
- Lifecycle modeling
- Webhook event mapping
- Seed data and field realism
- Integration workflows that match how APIs are actually used
- Developer trust from narrow, technically accurate content

This is hard for no-code or generic AI founders to copy because the product requires empathy for the integration debugging loop.

## Distribution Strategy

GitHub-first distribution:

- Keep the CLI and examples visible on GitHub/npm
- Earn contributor badges by helping API/tooling repos
- Publish useful engineering posts that cite real provider docs/specs
- Build credibility before linking the product

Content-first distribution:

- Dev.to for immediate search visibility
- Hashnode for cross-post backlink value
- fetchsandbox.com/blog for long-term domain authority
- X for build-in-public, short technical launch notes, and replies to pain threads
- Reddit is listening-only during account cooldown

The content should not be generic. Every article should follow:

`Specific provider + specific workflow + specific failure mode + production-grade fix + how to test it`

Examples:

- `Test Polar's customer and product API flow without an API token`
- `Twilio status callbacks: why delivered, failed, and undelivered need reconciliation`
- `The insert-first webhook idempotency pattern`
- `Paddle scheduled_change: the subscription field that breaks naive access control`
- `GitHub webhooks only retry a few times: design your handler like events can disappear`

## Pricing Hypothesis

Keep early access free while usage and workflows are being validated.

Likely pricing shape later:

- Free: public catalog, limited custom sandboxes, browser testing
- Pro: more custom specs, longer retention, CLI/CI workflows, private portals
- Team: shared workspaces, team analytics, custom domains, SSO, more retention

The Reddit post's useful pricing cue: devtools can work at low ACV when gross margins are high and churn is low because the tool becomes part of the workflow.

FetchSandbox should avoid pricing before activation is proven. Activation = user runs a workflow or sends a successful sandbox request, not just pageview.

## Current Product Details To Preserve

Polar onboarding:

- Spec: Polar API, `api.polar.sh/openapi.json`
- Config path: `/Users/raj/sandbox/backend/configs/polar/`
- Workflows:
  - `create_customer`: `POST /v1/customers/` then `GET /v1/customers/{id}`
  - `create_product`: `POST /v1/products/` then `GET /v1/products/`
- Webhook events:
  - `customer.created`
  - `product.created`
  - plus subscription/order/checkout/benefit events in config
- Content drafted:
  - `/Users/raj/sandbox/docs/blog/test-polar-customer-product-flow-without-api-token.md`
  - `/Users/raj/sandbox/docs/blog/x-posts-polar-launch.md`

## Phalanx Work Orders

Use these as ready prompts.

### Work Order: Add Product Positioning To FetchSandbox Site

```text
/phalanx build "In /Users/raj/sandbox, improve FetchSandbox homepage positioning using the product brief in /Users/raj/forge/docs/fetchsandbox-product-brief.md. Add a concise section that explains: API integrations fail in the lifecycle, not the endpoint; static mocks don't preserve state; FetchSandbox proves workflows with state and webhooks. Keep copy developer-native, no hype. Do not change brand styling. Add tests or lint validation if the repo has them."
```

### Work Order: Add Polar Launch Page/Internal Links

```text
/phalanx build "In /Users/raj/sandbox, add internal links for the new Polar sandbox. Link the homepage catalog/guide area and relevant docs/blog surfaces to /docs/polar. Use the Polar details from /Users/raj/forge/docs/fetchsandbox-product-brief.md. Keep scope narrow and avoid unrelated refactors."
```

### Work Order: Build A Founder/Product Page

```text
/phalanx build "In /Users/raj/sandbox, create a lightweight /about or /why page for FetchSandbox based on /Users/raj/forge/docs/fetchsandbox-product-brief.md. The page should explain the founder/technical thesis: OpenAPI gives endpoints, but integrations need lifecycle state, webhooks, and workflows. Include no fake testimonials and no inflated claims."
```

### Work Order: Add SEO Content Index

```text
/phalanx build "In /Users/raj/sandbox, add a blog/content index entry for the Polar article in docs/blog/test-polar-customer-product-flow-without-api-token.md if the site has a blog system. Preserve the existing blog conventions, metadata, and sitemap behavior."
```

## Guardrails

- Do not claim FetchSandbox exactly replicates vendor-specific payment logic.
- Do not claim SOC 2, GDPR, enterprise readiness, or production parity unless implemented.
- Do not use fake social proof.
- Do not write generic SEO pages without a specific provider/workflow/failure mode.
- Do not use Reddit for direct promotion during cooldown.
