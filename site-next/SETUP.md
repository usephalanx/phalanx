# site-next Setup

## Install dependencies

```bash
cd site-next
npm install
```

This generates `package-lock.json` and `node_modules/` — both are gitignored.

## Development

```bash
npm run dev       # http://localhost:3000
npm run build     # static export to out/
npm run lint      # ESLint via next lint
```

## Testing

```bash
npm test              # run all tests
npm run test:watch    # watch mode
npm run test:coverage # with coverage report
```

## Notes

- `next-env.d.ts` is auto-generated on first `npm run dev` or `npm run build` — do not commit manually.
- Static export (`output: 'export'`) produces `out/` directory for deploy to usephalanx.com.
