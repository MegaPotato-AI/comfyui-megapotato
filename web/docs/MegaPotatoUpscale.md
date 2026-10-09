# MegaPotato Upscale (API)

Upscales the image ×1, ×2, ×4 or ×8 with the MegaPotato AI upscaler ([megapotato.app](https://megapotato.app)).
The photo is uploaded to MegaPotato, processed on its GPUs, and the finished PNG comes back as the
`image` output.

## Setup

1. Sign in at [megapotato.app/account](https://megapotato.app/account) and create an API key.
2. Set the environment variable `MEGAPOTATO_API_KEY` before starting ComfyUI
   (or save the key in `megapotato_api_key.txt` inside the `comfyui-megapotato` folder).

Don't paste the key into the node unless you must: a widget value is stored in the workflow
JSON and in the metadata of every PNG you save.

## Inputs

- **image** — the photo(s). A batch becomes one job per image (up to 4 run in parallel).
- **upscale** — `1` enhances at the same size; `2`, `4`, `8` enlarge. Input up to 6000 px per side;
  output up to 16 400 px per side, so ×8 needs an input of 2050 px or less.
- **api_key_override** — only if you can't use the environment variable.
- **timeout_minutes** — how long to wait. Allow an extra 2–3 min when capacity is being brought
  online; a warm ×8 of a 1 MP photo takes about 45 s.
- **rerun** — the same photo at the same factor is never charged twice (the earlier result is
  returned). Change this number to force a fresh run.

## Outputs

- **image** — the upscaled image(s).
- **job_id** — the MegaPotato job id(s); the same job appears in your history on the site.
- **cost_chips** — chips charged for this run.

## Cost

Priced by the size of the photo and the factor, the same as on the site; the node prints the exact
price before uploading. Chips are held at submit and refunded automatically if the job fails.
Prices and packs: [megapotato.app/pricing](https://megapotato.app/pricing).

## Errors you may see

- *No MegaPotato API key* — set `MEGAPOTATO_API_KEY` and restart ComfyUI.
- *401 authentication_error* — the key is wrong, revoked or expired.
- *402 insufficient_credits* — top up chips.
- *over the output limit* — pick a smaller factor or a smaller input.
- *429 spend_limit_reached* — the account's daily API limit; it resets at 00:00 UTC.

Full API documentation: [megapotato.app/developers](https://megapotato.app/developers).
