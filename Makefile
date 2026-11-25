include wg21/flat.mk

# Landing page for GitHub Pages, generated from README.md and paper front matter.
# Phony so CI never deploys a stale page because of checkout mtimes.
.PHONY: index
index: tools/make_index.py | $(PYTHON_DIR) $(OUTDIR)
	$(PYTHON_BIN) tools/make_index.py . $(OUTDIR)/index.html
