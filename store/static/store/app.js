(() => {
  const tg = window.Telegram?.WebApp;
  const root = document.documentElement;
  const themeButton = document.getElementById('theme-toggle');
  function applyTheme() {
    if (tg?.initData) {
      root.dataset.telegram = 'on';
      const p = tg.themeParams || {};
      root.style.setProperty('--tg-bg', p.bg_color || '#f7f6f2');
      root.style.setProperty('--tg-secondary-bg', p.secondary_bg_color || p.bg_color || '#fff');
      root.style.setProperty('--tg-text', p.text_color || '#142120');
      root.style.setProperty('--tg-hint', p.hint_color || '#6f7a76');
      root.style.setProperty('--tg-button', p.button_color || '#e7fb51');
      themeButton.hidden = true;
      tg.ready(); tg.expand();
    } else {
      root.dataset.theme = localStorage.getItem('tok-theme') || 'light';
    }
  }
  applyTheme();
  tg?.onEvent?.('themeChanged', applyTheme);
  themeButton?.addEventListener('click', () => { const next = root.dataset.theme === 'dark' ? 'light' : 'dark'; root.dataset.theme = next; localStorage.setItem('tok-theme', next); });
  document.querySelectorAll('.tg-init-data').forEach(el => { el.value = tg?.initData || ''; });
  const startParam = tg?.initData ? new URLSearchParams(tg.initData).get('start_param') : new URLSearchParams(location.search).get('tgWebAppStartParam');
  if (location.pathname === '/' && /^product_[a-z0-9-]+$/.test(startParam || '')) {
    const slug = startParam.slice(8);
    location.replace(`/product/${encodeURIComponent(slug)}/`);
  }
  const storageKey = 'tok-cart-v1';
  if (location.pathname.startsWith('/order/') && new URLSearchParams(location.search).has('created')) { localStorage.removeItem(storageKey); history.replaceState(null, '', location.pathname); }
  function cart() { try { const value = JSON.parse(localStorage.getItem(storageKey) || '{}'); return value && typeof value === 'object' && !Array.isArray(value) ? value : {}; } catch { return {}; } }
  function saveCart(value) { localStorage.setItem(storageKey, JSON.stringify(value)); updateCount(); }
  function updateCount() { document.getElementById('cart-count').textContent = Object.values(cart()).reduce((sum, n) => sum + (+n || 0), 0); }
  updateCount();
  document.querySelectorAll('.add-to-cart').forEach(button => button.addEventListener('click', () => { const value = cart(); const id = button.dataset.productId; value[id] = Math.min(20, (+value[id] || 0) + 1); saveCart(value); button.textContent = 'Добавлено ✓'; setTimeout(() => button.innerHTML = 'В корзину <span>↗</span>', 1500); }));
  document.querySelectorAll('.gallery-thumb').forEach(button => button.addEventListener('click', () => { const current = document.getElementById('gallery-current'); current.src = button.dataset.image; current.alt = button.dataset.alt; document.querySelectorAll('.gallery-thumb').forEach(x => x.classList.remove('active')); button.classList.add('active'); }));
  const csrf = document.cookie.split('; ').find(x => x.startsWith('csrftoken='))?.split('=')[1];
  async function quote() {
    const response = await fetch('/api/cart/quote/', {method:'POST', headers:{'Content-Type':'application/json','X-CSRFToken':decodeURIComponent(csrf || ''), ...(tg?.initData ? {'X-Telegram-Init-Data':tg.initData} : {})}, body:JSON.stringify({cart:cart()})});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Не удалось проверить корзину');
    return data;
  }
  const money = value => `${Math.round(+value).toLocaleString('ru-RU')} ₽`;
  const basketBox = document.getElementById('basket-content');
  async function renderBasket() {
    if (!basketBox) return;
    if (!Object.keys(cart()).length) { basketBox.innerHTML = '<p class="empty-state">В корзине пока пусто. <a href="/">Посмотреть каталог ↗</a></p>'; document.getElementById('basket-total').textContent = '0 ₽'; document.getElementById('checkout-link').hidden = true; return; }
    try {
      const data = await quote(); basketBox.replaceChildren();
      for (const item of data.items) {
        const row = document.createElement('div'); row.className='basket-row';
        const photo = document.createElement('img'); photo.src=item.image; photo.alt=item.name; row.append(photo);
        const description=document.createElement('div'); const title=document.createElement('h3'); title.textContent=item.name; const small=document.createElement('small'); small.textContent=money(item.price); description.append(title,small); row.append(description);
        const control=document.createElement('div'); control.className='qty-control'; const minus=document.createElement('button'); minus.type='button'; minus.textContent='−'; const number=document.createElement('span'); number.textContent=item.quantity; const plus=document.createElement('button'); plus.type='button'; plus.textContent='+'; control.append(minus,number,plus); row.append(control);
        const remove=document.createElement('button'); remove.type='button'; remove.className='remove'; remove.textContent='×'; remove.setAttribute('aria-label',`Убрать ${item.name}`); row.append(remove);
        const change=(n)=>{const value=cart(); if(n<=0) delete value[item.id]; else value[item.id]=Math.min(20,n); saveCart(value); renderBasket();};
        minus.onclick=()=>change(item.quantity-1); plus.onclick=()=>change(item.quantity+1); remove.onclick=()=>change(0);
        basketBox.append(row);
      }
      document.getElementById('basket-total').textContent=money(data.total); document.getElementById('checkout-link').hidden=false;
    } catch(error) { basketBox.innerHTML=''; const msg=document.createElement('p'); msg.className='notice notice-error'; msg.textContent=error.message; basketBox.append(msg); document.getElementById('checkout-link').hidden=true; }
  }
  renderBasket();
  const checkoutForm=document.getElementById('checkout-form');
  if (checkoutForm) {
    const delivery=checkoutForm.elements.delivery, payment=checkoutForm.elements.payment_method;
    const updateDelivery=()=>{document.getElementById('address-field').hidden=delivery.value==='pickup'; const receipt=payment.querySelector('option[value="receipt"]'); receipt.disabled=delivery.value==='region'; if(receipt.disabled && payment.value==='receipt') payment.value='online';};
    delivery.addEventListener('change',updateDelivery); updateDelivery();
    checkoutForm.elements.cart.value=JSON.stringify(cart());
    quote().then(data=>{const box=document.getElementById('checkout-summary'); box.replaceChildren(); for(const item of data.items){const row=document.createElement('div'); row.className='summary-row'; const label=document.createElement('span'); label.textContent=`${item.name} × ${item.quantity}`; const price=document.createElement('strong'); price.textContent=money(item.subtotal); row.append(label,price); box.append(row);} document.getElementById('checkout-total').textContent=money(data.total);}).catch(error=>{document.getElementById('checkout-summary').textContent=error.message; checkoutForm.querySelector('.submit-order').disabled=true;});
    checkoutForm.addEventListener('submit',()=>{checkoutForm.elements.cart.value=JSON.stringify(cart()); checkoutForm.querySelector('.submit-order').disabled=true;});
    const contact=document.getElementById('request-contact');
    if(tg?.initData && tg.requestContact){contact.hidden=false; contact.addEventListener('click',()=>{tg.requestContact(async granted=>{const note=document.getElementById('contact-note'); note.hidden=false; if(!granted){note.textContent='Контакт не передан. Введите номер вручную.';return;} note.textContent='Ожидаем контакт от бота. Номер можно ввести вручную.'; for(let i=0;i<4;i++){await new Promise(resolve=>setTimeout(resolve,800)); try{const response=await fetch('/api/me/contact/',{headers:{'X-Telegram-Init-Data':tg.initData}}); if(response.ok){const data=await response.json(); if(data.phone){checkoutForm.elements.phone.value=data.phone; note.textContent='Номер получен от бота. Проверьте его перед заказом.';return;}}}catch{}} });});}
  }
  const payLink=document.getElementById('external-pay-link');
  if(payLink && tg?.initData && tg.openLink){payLink.addEventListener('click',event=>{event.preventDefault(); tg.openLink(payLink.href);});}
})();
