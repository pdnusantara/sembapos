import re

with open('app/templates/pos.html', 'r') as f:
    content = f.read()

content = content.replace(".querySelector(`.product-card[data-id=\"${id}\"]`)", ".querySelector(`.pos-product-card[data-id=\"${id}\"]`)")
content = content.replace("document.querySelectorAll('#productGrid .product-card')", "document.querySelectorAll('#productGrid .pos-product-card')")
content = content.replace("document.querySelectorAll('.product-card')", "document.querySelectorAll('.pos-product-card')")
content = content.replace("card.querySelector('.product-stock')", "card.querySelector('.pos-product-stock')")

with open('app/templates/pos.html', 'w') as f:
    f.write(content)
