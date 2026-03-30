import re

with open('app/templates/pos.html', 'r') as f:
    content = f.read()

new_sync = """function syncPosMobileLayout() {
  const productsArea = document.getElementById('posProductsArea');
  const cartArea = document.getElementById('posCartArea');
  if (!productsArea || !cartArea) return;
  
  if (window.innerWidth > 768) {
    productsArea.style.display = 'flex';
    cartArea.classList.remove('active');
  } else {
    const isCartActive = document.getElementById('posMobileTabCart') && document.getElementById('posMobileTabCart').classList.contains('active');
    if (isCartActive) {
      productsArea.style.display = 'none';
      cartArea.classList.add('active');
    } else {
      productsArea.style.display = 'flex';
      cartArea.classList.remove('active');
    }
  }
}"""

# Replace old syncPosMobileLayout
content = re.sub(r'function syncPosMobileLayout\(\) \{.*?\n\}', new_sync, content, flags=re.DOTALL)

# Remove the extra script block at the bottom
content = re.sub(r'// Ensure layout resets correctly on resize\nwindow\.addEventListener\(\'resize\', \(\) => \{.*?\n\}\);\n', '', content, flags=re.DOTALL)

with open('app/templates/pos.html', 'w') as f:
    f.write(content)
