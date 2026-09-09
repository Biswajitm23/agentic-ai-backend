#!/usr/bin/env bash
# Push ONLY the home page template to the LIVE LittleNest theme (#157992386733).
# Everything else in the theme is untouched.
#
# To revert, the previous live version is backed up at:
#   $TMP/scratchpad/index.json.backup   (see message for full path)
shopify theme push \
  --store palashstor.myshopify.com \
  --theme 157992386733 \
  --only templates/index.json \
  --allow-live
