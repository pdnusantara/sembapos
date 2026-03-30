import re

with open('app/templates/pos.html', 'r') as f:
    content = f.read()

# Extract the script that is outside the block
pattern = r'\{% endblock %\}\n*\<script\>\nfunction toggleMobileView.*?\</script\>'
match = re.search(pattern, content, flags=re.DOTALL)

if match:
    script_content = match.group(0).replace('{% endblock %}', '').replace('<script>', '').replace('</script>', '').strip()
    
    # Remove the bad block
    content = content[:match.start()] + '\n{% endblock %}'
    
    # Insert the script content before the last {% endblock %} which is for scripts
    # Wait, the last {% endblock %} IS the scripts block.
    # Let's find the end of the script block.
    script_end_pattern = r'\n</script>\n\{% endblock %\}'
    content = re.sub(script_end_pattern, f'\n\n{script_content}\n</script>\n{{% endblock %}}', content)

    with open('app/templates/pos.html', 'w') as f:
        f.write(content)
    print("Fixed script block")
else:
    print("Pattern not found")
