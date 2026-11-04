"""Sphinx configuration for SpiriSynq.

The docs are a controlled document, like every Spiri manual: the spiri_docs
extension reads revisions.yaml on every build, validates it, sets the revision,
stamps it into the PDF footer, and names the PDF after it. Theme and structure
live here, because they are decided once per project.

Build with `make docs` (HTML) and `make pdf`.
"""

from __future__ import annotations

project = "SpiriSynq"
author = "Spiri Robotics"
copyright = "%Y, Spiri Robotics"  # noqa: A001 - the name Sphinx expects

# `version` and `release` are deliberately not set here. spiri_docs sets both
# from the current entry in revisions.yaml.

extensions = [
    "myst_parser",
    "spiri_docs",
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "sphinx.ext.intersphinx",
]

exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

# -- Prose ------------------------------------------------------------------

# `substitution` is what makes {{ document_number }} and {{ revision }} work in
# Markdown; the values come from revisions.yaml via spiri_docs.
myst_enable_extensions = [
    "colon_fence",
    "deflist",
    "substitution",
]
myst_heading_anchors = 3

# -- API reference ----------------------------------------------------------

autodoc_default_options = {
    "members": True,
    "undoc-members": True,
    "show-inheritance": True,
}
autodoc_member_order = "bysource"
autodoc_typehints = "description"
napoleon_google_docstring = True
napoleon_numpy_docstring = True
# The optional `examples` group's imports, so the docs build without it.
autodoc_mock_imports = ["PIL", "cv2", "nicegui", "imagecodecs"]

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
}

# -- HTML -------------------------------------------------------------------

html_theme = "furo"
html_static_path = ["_static"]
html_css_files = ["custom.css"]
html_title = "SpiriSynq"
# Versions are published side by side (.github/scripts/publish_docs.py), and
# latest/ is always the newest release: the one a search engine should show.
html_baseurl = "https://spiri-robotics.github.io/SpiriSynq/latest/"

# Furo's sidebar, with the version switcher and PDF link (_templates/versions.html)
# under the search box.
templates_path = ["_templates"]
html_sidebars = {
    "**": [
        "sidebar/brand.html",
        "sidebar/search.html",
        "versions.html",
        "sidebar/scroll-start.html",
        "sidebar/navigation.html",
        "sidebar/ethical-ads.html",
        "sidebar/scroll-end.html",
        "sidebar/variant-selector.html",
    ]
}

html_theme_options = {
    "navigation_with_keys": True,
    # Two files, not one tinted by CSS: the wordmark is black in one and white in
    # the other, and `filter: invert()` on a multi-coloured mark would take the
    # brand colours with it. Furo swaps these on the theme toggle.
    "light_logo": "spiri-logo-light.svg",
    "dark_logo": "spiri-logo-dark.svg",
}

# -- PDF --------------------------------------------------------------------
# The cover logo. LaTeX cannot read SVG, so spiri_docs converts this one to PDF
# during a LaTeX build. The light variant is correct: a cover is printed.
spiri_docs_logo = "_static/spiri-logo-light.svg"
# spiri_docs sets latex_documents (so the file is named for the document number
# and revision) and appends the document-control preamble. What is left here is
# the engine and page setup.
#
# XeLaTeX rather than pdflatex: it reads Unicode natively, so a docstring with
# "∘" or "→" in it is typeset instead of stopping the build. pdflatex knows only
# the characters it has been told about, one at a time.
latex_engine = "xelatex"
latex_elements = {
    "papersize": "letterpaper",
    "pointsize": "11pt",
    "figure_align": "htbp",
}
