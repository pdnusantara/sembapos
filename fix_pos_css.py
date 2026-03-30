import re

with open('app/static/css/pos-modern.css', 'r') as f:
    css = f.read()

# Make .pos-layout height !important
css = re.sub(r'(\.pos-layout\s*{[^}]*?height:\s*[^;]+);', r'\1 !important;', css)
css = re.sub(r'(\.pos-layout\s*{[^}]*?grid-template-columns:\s*[^;]+);', r'\1 !important;', css)

with open('app/static/css/pos-modern.css', 'w') as f:
    f.write(css)
