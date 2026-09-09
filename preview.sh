#!/usr/bin/env bash
# Safer alternative to push.sh: publish the theme to a NEW UNPUBLISHED theme so
# you can preview the home page change before it touches the live storefront.
# The live theme is not modified. The CLI prints a preview URL when it finishes.
shopify theme push \
  --store palashstor.myshopify.com \
  --unpublished \
  --theme "LittleNest + collections preview"
