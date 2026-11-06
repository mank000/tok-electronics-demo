(() => {
  const tg = window.Telegram?.WebApp;
  const root = document.documentElement;
  const themeButton = document.getElementById('theme-toggle');
  document.querySelectorAll('[data-telegram-link]').forEach(link => link.addEventListener('click', event => {
    if (tg?.initData && tg.openTelegramLink) {
      event.preventDefault();
      tg.openTelegramLink(link.href);
    }
  }));
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
  const compareKey = 'tok-compare-v1';
  const validSlug = value => typeof value === 'string' && /^[a-z0-9-]{1,50}$/.test(value);
  function cleanCompare(value) {
    if (!Array.isArray(value)) return [];
    const items = [], seen = new Set(), counts = {};
    for (const item of value) {
      if (!item || !validSlug(item.slug) || !validSlug(item.category) || seen.has(item.slug) || (counts[item.category] || 0) >= 4) continue;
      items.push({slug:item.slug, category:item.category}); seen.add(item.slug); counts[item.category]=(counts[item.category] || 0)+1;
      if (items.length === 20) break;
    }
    return items;
  }
  let compared = [];
  try { compared = cleanCompare(JSON.parse(localStorage.getItem(compareKey) || '[]')); } catch {}
  const compareUrl = (items, category='') => {
    if (!items.length) return '/compare/';
    const params = new URLSearchParams({items:items.map(item => item.slug).join(',')});
    if (category) params.set('category', category);
    return `/compare/?${params}`;
  };
  const comparePage = document.getElementById('compare-page');
  if (comparePage) {
    const params = new URLSearchParams(location.search);
    if (params.has('items')) {
      try { compared = cleanCompare(JSON.parse(document.getElementById('compare-page-items').textContent)); localStorage.setItem(compareKey, JSON.stringify(compared)); } catch {}
    } else if (compared.length) location.replace(compareUrl(compared));
  }
  function syncCompare() {
    const nav = document.querySelector('.compare-nav'), count = document.getElementById('compare-count');
    if (nav) nav.href = compareUrl(compared);
    if (count) { count.textContent = compared.length; count.hidden = !compared.length; }
    for (const button of document.querySelectorAll('[data-compare-slug]')) {
      const selected = compared.some(item => item.slug === button.dataset.compareSlug);
      button.setAttribute('aria-pressed', String(selected)); button.textContent = selected ? '✓ В сравнении' : '⊕ Сравнить';
    }
    if (!comparePage) {
      let tray = document.getElementById('compare-tray');
      if (compared.length && !tray) { tray = document.createElement('div'); tray.id='compare-tray'; tray.className='compare-tray'; tray.setAttribute('role','status'); document.body.append(tray); }
      if (tray) { tray.hidden = !compared.length; tray.replaceChildren(); if (compared.length) { const label=document.createElement('strong'); label.textContent=`В сравнении: ${compared.length}`; const link=document.createElement('a'); link.href=compareUrl(compared); link.textContent='Сравнить ↗'; tray.append(label,link); } }
    }
  }
  function saveCompare(items) {
    compared = cleanCompare(items);
    try { localStorage.setItem(compareKey, JSON.stringify(compared)); } catch {}
    syncCompare();
  }
  function compareNotice(message) {
    let notice = document.getElementById('compare-notice');
    if (!notice) { notice = document.createElement('p'); notice.id='compare-notice'; notice.className='compare-notice'; notice.setAttribute('role','status'); document.body.append(notice); }
    notice.textContent = message; notice.hidden = false;
    clearTimeout(compareNotice.timer); compareNotice.timer = setTimeout(() => { notice.hidden=true; }, 3500);
  }
  syncCompare();
  document.addEventListener('click', event => {
    const button = event.target.closest('[data-compare-slug]');
    if (!button) return;
    const slug=button.dataset.compareSlug, category=button.dataset.compareCategory;
    if (compared.some(item => item.slug===slug)) { saveCompare(compared.filter(item => item.slug!==slug)); return; }
    if (compared.filter(item => item.category===category).length>=4) { compareNotice('Можно сравнить до четырёх товаров из одного раздела.'); return; }
    saveCompare([...compared,{slug,category}]);
  });
  if (comparePage) {
    const selectedCategory = comparePage.dataset.category;
    document.querySelectorAll('[data-compare-remove]').forEach(button => button.addEventListener('click', () => { const items=compared.filter(item => item.slug!==button.dataset.compareRemove); saveCompare(items); location.assign(compareUrl(items, selectedCategory)); }));
    document.getElementById('compare-clear-category')?.addEventListener('click', () => { const items=compared.filter(item => item.category!==selectedCategory); saveCompare(items); location.assign(compareUrl(items)); });
    document.getElementById('compare-clear-all')?.addEventListener('click', () => { saveCompare([]); location.assign('/compare/?items='); });
    document.getElementById('compare-only-differences')?.addEventListener('change', event => { comparePage.classList.toggle('compare-differences-only', event.target.checked); });
  }
  window.addEventListener('storage', event => { if (event.key===compareKey) { try { compared=cleanCompare(JSON.parse(event.newValue || '[]')); } catch { compared=[]; } syncCompare(); } });
  const catalogSection = document.getElementById('catalog');
  if (catalogSection) {
    let pendingRequest;
    let inputTimer;
    let currentCategory = new URL(location.href).searchParams.get('category') || '';
    const formUrl = () => {
      const form = catalogSection.querySelector('.catalog-filter-form');
      const url = new URL(form.action, location.href);
      const params = new URLSearchParams(new FormData(form));
      for (const [key, value] of [...params]) if (!value.trim()) params.delete(key);
      url.search = params.toString();
      return url;
    };
    async function updateCatalog(url, replaceForm = false, fromHistory = false) {
      clearTimeout(inputTimer);
      const target = new URL(url, location.href);
      if (target.origin !== location.origin || target.pathname !== location.pathname) { location.assign(target.href); return; }
      pendingRequest?.abort();
      const request = new AbortController();
      pendingRequest = request;
      catalogSection.querySelector('.catalog-results').setAttribute('aria-busy', 'true');
      try {
        const response = await fetch(target.href, {signal: request.signal});
        if (!response.ok) throw new Error('Не удалось обновить каталог');
        const next = new DOMParser().parseFromString(await response.text(), 'text/html');
        const nextCatalog = next.getElementById('catalog');
        const nextResults = nextCatalog?.querySelector('.catalog-results');
        const nextCount = nextCatalog?.querySelector('.catalog-result-count');
        if (!nextResults || !nextCount) throw new Error('Не удалось прочитать каталог');
        const nextCategory = target.searchParams.get('category') || '';
        if (replaceForm || nextCategory !== currentCategory) {
          const wasOpen = catalogSection.querySelector('.catalog-filters').open;
          catalogSection.querySelector('.category-list').replaceWith(nextCatalog.querySelector('.category-list'));
          const nextForm = nextCatalog.querySelector('.catalog-filter-form');
          nextForm.querySelector('.catalog-filters').open = wasOpen || nextForm.querySelector('.catalog-filters').open;
          catalogSection.querySelector('.catalog-filter-form').replaceWith(nextForm);
        } else {
          catalogSection.querySelector('.catalog-filters summary').innerHTML = nextCatalog.querySelector('.catalog-filters summary').innerHTML;
          catalogSection.querySelector('.catalog-filter-error').innerHTML = nextCatalog.querySelector('.catalog-filter-error').innerHTML;
        }
        catalogSection.querySelector('.catalog-result-count').textContent = nextCount.textContent;
        catalogSection.querySelector('.catalog-results').replaceWith(nextResults);
        document.title = next.title;
        for (const selector of ['meta[name="description"]', 'meta[name="robots"]', 'link[rel="canonical"]', 'meta[property="og:title"]', 'meta[property="og:description"]', 'meta[property="og:url"]']) {
          const oldTag = document.head.querySelector(selector), newTag = next.head.querySelector(selector);
          if (oldTag && newTag) oldTag.replaceWith(newTag);
          else if (oldTag) oldTag.remove();
          else if (newTag) document.head.append(newTag);
        }
        currentCategory = nextCategory;
        syncCompare();
        if (!fromHistory && target.href !== location.href) history.pushState(null, '', target.href);
      } catch (error) {
        if (error.name !== 'AbortError') location.assign(target.href);
      } finally {
        if (pendingRequest === request) {
          pendingRequest = null;
          catalogSection.querySelector('.catalog-results').removeAttribute('aria-busy');
        }
      }
    }
    catalogSection.addEventListener('submit', event => {
      if (!event.target.matches('.catalog-filter-form')) return;
      event.preventDefault();
      updateCatalog(formUrl());
    });
    catalogSection.addEventListener('change', event => {
      if (event.target.closest('.catalog-filter-form')) updateCatalog(formUrl());
    });
    catalogSection.addEventListener('input', event => {
      if (!event.target.matches('input[name="q"], input[name="price_from"], input[name="price_to"]')) return;
      clearTimeout(inputTimer);
      inputTimer = setTimeout(() => { if (event.target.reportValidity()) updateCatalog(formUrl()); }, 350);
    });
    catalogSection.addEventListener('click', event => {
      const link = event.target.closest('.category-list a, .filter-actions a');
      if (!link || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
      event.preventDefault();
      updateCatalog(link.href, true);
    });
    window.addEventListener('popstate', () => updateCatalog(location.href, true, true));
  }
  document.querySelectorAll('.gallery-thumb').forEach(button => button.addEventListener('click', () => { const current = document.getElementById('gallery-current'); current.src = button.dataset.image; current.alt = button.dataset.alt; document.querySelectorAll('.gallery-thumb').forEach(x => x.classList.remove('active')); button.classList.add('active'); }));
  const csrf = document.cookie.split('; ').find(x => x.startsWith('csrftoken='))?.split('=')[1];
  async function quote(promoCode='') {
    const response = await fetch('/api/cart/quote/', {method:'POST', headers:{'Content-Type':'application/json','X-CSRFToken':decodeURIComponent(csrf || ''), ...(tg?.initData ? {'X-Telegram-Init-Data':tg.initData} : {})}, body:JSON.stringify({cart:cart(), coupon_code:promoCode})});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Не удалось проверить корзину');
    return data;
  }
  const money = value => { const amount=Number(value); return `${amount.toLocaleString('ru-RU',{minimumFractionDigits:Number.isInteger(amount)?0:2,maximumFractionDigits:2})} ₽`; };
  const basketBox = document.getElementById('basket-content');
  async function renderBasket() {
    if (!basketBox) return;
    if (!Object.keys(cart()).length) { basketBox.innerHTML = '<p class="empty-state">В корзине пока пусто. <a href="/">Посмотреть каталог ↗</a></p>'; document.getElementById('basket-total').textContent = '0 ₽'; document.getElementById('checkout-link').hidden = true; return; }
    try {
      const data = await quote(); basketBox.replaceChildren();
      for (const item of data.items) {
        const row = document.createElement('div'); row.className='basket-row';
        const photo = document.createElement('img'); photo.src=item.image; photo.alt=item.name; row.append(photo);
        const description=document.createElement('div'); const title=document.createElement('h3'); title.textContent=item.name; const small=document.createElement('small'); small.textContent=money(item.price); description.append(title,small); if(+item.list_price>+item.price){const old=document.createElement('del');old.className='old-price';old.textContent=money(item.list_price);description.append(old);} row.append(description);
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
    if (tg?.initData || document.body.dataset.sessionAuth==='true') document.getElementById('name-field').hidden=true;
    const updateDelivery=()=>{document.getElementById('address-field').hidden=delivery.value==='pickup'; const receipt=payment.querySelector('option[value="receipt"]'); receipt.disabled=delivery.value==='region'; if(receipt.disabled && payment.value==='receipt') payment.value='online'; document.getElementById('gateway-field').hidden=payment.value!=='online';};
    delivery.addEventListener('change',updateDelivery); payment.addEventListener('change',updateDelivery); updateDelivery();
    checkoutForm.elements.cart.value=JSON.stringify(cart());
    const summaryRow=(box,label,value)=>{const row=document.createElement('div');row.className='summary-row';const name=document.createElement('span');name.textContent=label;const amount=document.createElement('strong');amount.textContent=value;row.append(name,amount);box.append(row);};
    const renderCheckout=async()=>{
      const note=document.getElementById('coupon-message'), button=checkoutForm.querySelector('.submit-order');
      try {
        const data=await quote(checkoutForm.elements.coupon_code.value);
        const box=document.getElementById('checkout-summary'), discounts=document.getElementById('checkout-discounts');box.replaceChildren();discounts.replaceChildren();
        for(const item of data.items)summaryRow(box,`${item.name} × ${item.quantity}`,money(item.subtotal));
        if(+data.product_discount)summaryRow(discounts,'Скидка на товары',`−${money(data.product_discount)}`);
        if(+data.coupon_discount)summaryRow(discounts,`Промокод ${data.coupon_code}`,`−${money(data.coupon_discount)}`);
        document.getElementById('checkout-total').textContent=money(data.total);note.textContent=data.coupon_code?'Промокод применён.':' ';button.disabled=false;
      } catch(error) {note.textContent=error.message;button.disabled=true;}
    };
    renderCheckout();
    document.getElementById('apply-coupon')?.addEventListener('click',renderCheckout);
    checkoutForm.elements.coupon_code.addEventListener('input',()=>{document.getElementById('coupon-message').textContent='Нажмите «Применить», чтобы пересчитать сумму.';});
    checkoutForm.addEventListener('submit',()=>{checkoutForm.elements.cart.value=JSON.stringify(cart()); checkoutForm.querySelector('.submit-order').disabled=true;});
    const contact=document.getElementById('request-contact');
    if(tg?.initData) fetch('/api/me/contact/',{headers:{'X-Telegram-Init-Data':tg.initData}}).then(r=>r.ok?r.json():null).then(data=>{if(data?.phone&&!checkoutForm.elements.phone.value)checkoutForm.elements.phone.value=data.phone;}).catch(()=>{});
    if(tg?.initData && tg.requestContact){contact.hidden=false; contact.addEventListener('click',()=>{tg.requestContact(async granted=>{const note=document.getElementById('contact-note'); note.hidden=false; if(!granted){note.textContent='Контакт не передан. Введите номер вручную.';return;} note.textContent='Ожидаем контакт от бота. Номер можно ввести вручную.'; for(let i=0;i<4;i++){await new Promise(resolve=>setTimeout(resolve,800)); try{const response=await fetch('/api/me/contact/',{headers:{'X-Telegram-Init-Data':tg.initData}}); if(response.ok){const data=await response.json(); if(data.phone){checkoutForm.elements.phone.value=data.phone; note.textContent='Номер получен от бота. Проверьте его перед заказом.';return;}}}catch{}} });});}
  }
  const identityHeaders = () => tg?.initData ? {'X-Telegram-Init-Data': tg.initData} : {};
  const registrationOverlay=document.getElementById('registration-overlay');
  if (tg?.initData && registrationOverlay && !sessionStorage.getItem('tok-registration-later')) {
    const form=document.getElementById('registration-form'), message=document.getElementById('registration-message');
    const contactButton=document.getElementById('registration-contact'), submitButton=document.getElementById('registration-submit');
    const status=async()=>{const response=await fetch('/api/me/registration/',{headers:identityHeaders()}); if(!response.ok) throw Error('Не удалось проверить регистрацию'); return response.json();};
    const applyStatus=data=>{if(!form.elements.name.value)form.elements.name.value=data.name||''; submitButton.disabled=!data.has_phone; contactButton.textContent=data.has_phone?'✓ Номер получен':'Поделиться номером через Telegram'; if(data.has_phone) message.textContent='Номер получен. Проверьте имя и сохраните.';};
    status().then(data=>{if(!data.registered){registrationOverlay.hidden=false; applyStatus(data);}}).catch(()=>{});
    contactButton.addEventListener('click',()=>{
      if(!tg.requestContact){message.textContent='Обновите Telegram, чтобы поделиться номером.';return;}
      contactButton.disabled=true;
      tg.requestContact(async granted=>{
        if(!granted){message.textContent='Вы не передали номер. Можно продолжить без регистрации.';contactButton.disabled=false;return;}
        message.textContent='Ждём номер от Telegram…';
        for(let i=0;i<20;i++){
          await new Promise(resolve=>setTimeout(resolve,1000));
          try{const data=await status();if(data.has_phone){applyStatus(data);contactButton.disabled=false;return;}}catch{}
        }
        message.textContent='Бот пока не получил контакт. Проверьте, что он запущен, и попробуйте ещё раз.';
        contactButton.disabled=false;
      });
    });
    form.addEventListener('submit',async event=>{
      event.preventDefault();submitButton.disabled=true;message.textContent='Сохраняем…';
      try{const body=new URLSearchParams({name:form.elements.name.value});const response=await fetch('/api/me/registration/',{method:'POST',headers:{'X-CSRFToken':decodeURIComponent(csrf||''),'Content-Type':'application/x-www-form-urlencoded',...identityHeaders()},body});const data=await response.json();if(!response.ok)throw Error(data.error||'Не удалось сохранить');location.reload();}
      catch(error){message.textContent=error.message;submitButton.disabled=false;}
    });
    document.getElementById('registration-later').addEventListener('click',()=>{registrationOverlay.hidden=true;sessionStorage.setItem('tok-registration-later','1');});
  }
  const favoriteButton = document.querySelector('.favorite-button');
  if (favoriteButton) {
    const slug = favoriteButton.dataset.slug;
    const label = selected => { favoriteButton.setAttribute('aria-pressed', String(selected)); favoriteButton.textContent = selected ? '♥ В избранном' : '♡ В избранное'; };
    fetch(`/api/product/${encodeURIComponent(slug)}/favorite/`, {headers: identityHeaders()}).then(r => r.ok ? r.json() : Promise.reject()).then(data => {
      label(data.favorite);
      const review=document.querySelector('.review-form'), note=document.getElementById('review-eligibility');
      if (review && note) { review.hidden=!data.can_review; note.hidden=!!data.can_review; note.textContent=!data.authenticated?'Войдите через Telegram, чтобы узнать, доступен ли отзыв.':data.already_reviewed?'Вы уже оставили отзыв об этом товаре.': 'Отзыв можно написать после покупки и доставки товара.'; }
      document.querySelectorAll('.auth-required').forEach(el => { if (!el.classList.contains('review-form')) el.hidden=!data.authenticated; });
      document.querySelectorAll('.auth-login').forEach(el => { el.hidden=!!data.authenticated; });
    }).catch(() => {});
    fetch(`/product/${encodeURIComponent(slug)}/viewed/`, {method:'POST',headers:{'X-CSRFToken':decodeURIComponent(csrf || ''),...identityHeaders()}}).catch(() => {});
    favoriteButton.addEventListener('click', async () => {
      favoriteButton.disabled = true;
      try { const response = await fetch(`/product/${encodeURIComponent(slug)}/favorite/`, {method:'POST', headers:{'X-CSRFToken':decodeURIComponent(csrf || ''), ...identityHeaders()}}); if(!response.ok) throw Error(); label((await response.json()).favorite); }
      catch { document.querySelector('.share-feedback').textContent='Войдите через Telegram, чтобы сохранить товар в избранном.'; }
      finally { favoriteButton.disabled = false; }
    });
  }
  document.querySelector('.share-product')?.addEventListener('click', async event => {
    const url = tg?.initData && event.currentTarget.dataset.miniappUrl ? event.currentTarget.dataset.miniappUrl : `${location.origin}${location.pathname}`;
    const feedback = document.querySelector('.share-feedback');
    if (tg?.initData && tg.openTelegramLink) {
      tg.openTelegramLink(`https://t.me/share/url?url=${encodeURIComponent(url)}&text=${encodeURIComponent('Посмотри этот товар')}`);
      feedback.textContent='Выберите чат в Telegram, чтобы отправить ссылку.';
    } else {
      try { await navigator.clipboard.writeText(url); feedback.textContent='Ссылка скопирована в буфер обмена.'; }
      catch { feedback.textContent=url; }
    }
  });
  const accountPage=document.querySelector('.account-page');
  if (accountPage) {
    const requestForm=document.getElementById('phone-login-request'), verifyForm=document.getElementById('phone-login-verify');
    const loginMessage=document.getElementById('phone-login-message');
    requestForm?.addEventListener('submit',async event=>{
      event.preventDefault();const button=requestForm.querySelector('button');button.disabled=true;
      try{const body=new URLSearchParams({phone:requestForm.elements.phone.value,next:new URLSearchParams(location.search).get('next')||location.pathname});const response=await fetch('/auth/phone/request/',{method:'POST',headers:{'X-CSRFToken':decodeURIComponent(csrf||''),'Content-Type':'application/x-www-form-urlencoded'},body});const data=await response.json();if(!response.ok)throw Error(data.error||'Не удалось запросить код');loginMessage.textContent=data.message;verifyForm.hidden=false;verifyForm.elements.code.focus();}
      catch(error){loginMessage.textContent=error.message;}finally{button.disabled=false;}
    });
    verifyForm?.addEventListener('submit',async event=>{
      event.preventDefault();const button=verifyForm.querySelector('button');button.disabled=true;
      try{const body=new URLSearchParams({code:verifyForm.elements.code.value});const response=await fetch('/auth/phone/verify/',{method:'POST',headers:{'X-CSRFToken':decodeURIComponent(csrf||''),'Content-Type':'application/x-www-form-urlencoded'},body});const data=await response.json();if(!response.ok)throw Error(data.error||'Неверный код');location.assign(data.redirect);}
      catch(error){loginMessage.textContent=error.message;verifyForm.elements.code.value='';button.disabled=false;}
    });
    const card = (heading, detail, url) => {
      const article=document.createElement('article'); article.className='account-card';
      const title=document.createElement('strong'); title.textContent=heading;
      const info=document.createElement('span'); info.textContent=detail;
      const link=document.createElement('a'); link.className='text-button'; link.href=url; link.textContent='Подробнее ↗';
      article.append(title, info, link); return article;
    };
    if (tg?.initData) document.querySelector('.account-logout')?.setAttribute('hidden','');
    fetch('/api/account/', {headers:identityHeaders()}).then(async response => {
      if(response.status===401) { document.getElementById('account-login').hidden=false; return null; }
      if(!response.ok) throw Error('Не удалось открыть кабинет');
      return response.json();
    }).then(data => {
      if (!data) return;
      document.getElementById('account-content').hidden=false;
      document.getElementById('account-greeting').textContent=`${data.profile.name}, рады вас видеть.`;
      for (const list of document.querySelectorAll('[data-account-list]')) {
        const type=list.dataset.accountList; list.replaceChildren();
        if(!data[type].length) { const empty=document.createElement('p'); empty.className='empty-state'; empty.textContent=type==='current'?'Текущих заказов пока нет.':type==='purchases'?'Завершённых покупок пока нет.':type==='wishes'?'Заявок пока нет.':'Сохранённых товаров пока нет.'; list.append(empty); continue; }
        for (const item of data[type]) {
          const detail=type==='current'||type==='purchases' ? `${item.created} · ${item.status} · ${money(item.total)} · ${item.items.map(x => x.name).join(', ')}` : type==='wishes' ? `Заявка от ${item.created}` : money(item.price);
          list.append(card(item.id ? `Заказ №${item.id}` : item.name, detail, item.url));
        }
      }
      const track=document.getElementById('recent-track');
      if (track) {
        track.replaceChildren();
        if (!data.recent.length) { const empty=document.createElement('p'); empty.className='empty-state'; empty.textContent='Вы ещё не смотрели товары.'; track.append(empty); }
        for (const item of data.recent) {
          const link=document.createElement('a'); link.href=item.url; link.className='recent-card';
          if(item.image) { const image=document.createElement('img'); image.src=item.image; image.alt=''; link.append(image); }
          const name=document.createElement('strong'); name.textContent=item.name;
          const price=document.createElement('span'); price.textContent=money(item.price);
          link.append(name, price); track.append(link);
        }
      }
    }).catch(error => { const box=document.getElementById('account-error'); box.hidden=false; box.textContent=error.message; });
  }
  const widgetBox=document.getElementById('payment-form');
  if(widgetBox) {
    try {
      const widget=new window.YooMoneyCheckoutWidget({confirmation_token:widgetBox.dataset.confirmationToken, return_url:new URL(`${widgetBox.dataset.returnUrl}?from=yoo`, location.origin).href, error_callback: error => { const box=document.getElementById('payment-error'); box.hidden=false; box.textContent='Виджет ЮKassa сообщил об ошибке. Попробуйте ещё раз или вернитесь к заказу.'; }});
      widget.render('payment-form').catch(() => { const box=document.getElementById('payment-error'); box.hidden=false; box.textContent='Не удалось загрузить форму оплаты.'; });
    } catch { const box=document.getElementById('payment-error'); box.hidden=false; box.textContent='Не удалось загрузить форму оплаты.'; }
  }
  document.querySelector('.check-yoo-status')?.addEventListener('click', async event => {
    const button=event.currentTarget, note=document.getElementById('yoo-status-message'); button.disabled=true; note.textContent='Проверяем платёж…';
    try { const response=await fetch(button.dataset.url,{method:'POST',headers:{'X-CSRFToken':decodeURIComponent(csrf || ''),...identityHeaders()}}); const data=await response.json(); if(!response.ok) throw Error(data.error || 'Ошибка проверки'); if(data.state==='paid'||data.state==='declined') { history.replaceState(null, '', location.pathname); location.reload(); } else note.textContent='Платёж пока ожидается.'; }
    catch(error){ note.textContent=error.message; } finally { button.disabled=false; }
  });
  if (new URLSearchParams(location.search).has('from') && document.querySelector('.check-yoo-status')) document.querySelector('.check-yoo-status').click();

})();
