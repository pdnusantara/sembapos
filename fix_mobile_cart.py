import re

with open('app/static/css/pos-modern.css', 'r') as f:
    css = f.read()

# Remove the fixed positioning for .pos-cart-container on mobile
css = re.sub(r'\.pos-cart-container\s*\{\s*display:\s*none;\s*position:\s*fixed;[^\}]*\}', 
             r'.pos-cart-container {\n    display: none;\n    flex: 1;\n    border-radius: 0;\n    border: none;\n    border-top: 1px solid var(--pos-border);\n  }', css)

with open('app/static/css/pos-modern.css', 'w') as f:
    f.write(css)
