import re

with open('app/templates/pos.html', 'r') as f:
    content = f.read()

script_content = """
function toggleMobileView(view) {
  const productsArea = document.getElementById('posProductsArea');
  const cartArea = document.getElementById('posCartArea');
  const tabProduk = document.getElementById('posMobileTabProduk');
  const tabCart = document.getElementById('posMobileTabCart');
  
  if (!productsArea || !cartArea) return;
  
  if (view === 'produk') {
    if (window.innerWidth <= 768) {
      productsArea.style.display = 'flex';
      cartArea.classList.remove('active');
    }
    if (tabProduk) tabProduk.classList.add('active');
    if (tabCart) tabCart.classList.remove('active');
  } else {
    if (window.innerWidth <= 768) {
      productsArea.style.display = 'none';
      cartArea.classList.add('active');
    }
    if (tabProduk) tabProduk.classList.remove('active');
    if (tabCart) tabCart.classList.add('active');
  }
}
"""

# Insert before the last </script>
idx = content.rfind('</script>')
if idx != -1:
    content = content[:idx] + script_content + '\n' + content[idx:]
    with open('app/templates/pos.html', 'w') as f:
        f.write(content)
    print("Inserted toggleMobileView")
