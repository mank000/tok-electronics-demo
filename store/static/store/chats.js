(() => {
  const page = document.querySelector('.chats-page');
  if (!page) return;

  const tg = window.Telegram?.WebApp;
  const identity = () => tg?.initData ? {'X-Telegram-Init-Data': tg.initData} : {};
  const csrf = decodeURIComponent(document.cookie.split('; ').find(value => value.startsWith('csrftoken='))?.split('=')[1] || '');
  const status = document.getElementById('chats-status');
  const login = document.getElementById('chats-login');
  const conversationId = page.dataset.conversationId;
  const productId = page.dataset.shareProductId;
  const nonce = () => globalThis.crypto?.randomUUID?.();
  const pendingShares = new Map();

  async function api(url, body) {
    const response = await fetch(url, {
      method: body === undefined ? 'GET' : 'POST',
      headers: {...identity(), ...(body === undefined ? {} : {'Content-Type': 'application/json', 'X-CSRFToken': csrf})},
      ...(body === undefined ? {} : {body: JSON.stringify(body)}),
    });
    const data = await response.json().catch(() => ({}));
    if (response.status === 401 || (response.status === 403 && !data.error)) {
      login.hidden = false;
      throw new Error(data.error || 'Войдите через Telegram');
    }
    if (!response.ok) throw new Error(data.error || 'Не удалось выполнить действие');
    return data;
  }

  function notice(message, error = false) {
    status.hidden = false;
    status.textContent = message;
    status.classList.toggle('notice-error', error);
  }

  async function sendProduct(chatId) {
    const caption = document.getElementById('chat-caption')?.value || '';
    const previous = pendingShares.get(chatId);
    const token = previous?.caption === caption ? previous.token : nonce();
    pendingShares.set(chatId, {caption, token});
    const result = await api(`/api/chats/${chatId}/send/`, {product_id: productId, text: caption, nonce: token});
    if (result.message) {
      pendingShares.delete(chatId);
      location.assign(`/messages/${chatId}/`);
    }
  }

  async function showList() {
    const data = await api('/api/chats/');
    document.getElementById('chat-list-view').hidden = false;
    const tagInput = document.getElementById('chat-own-tag');
    tagInput.value = data.tag ? `@${data.tag}` : '';
    const list = document.getElementById('chat-list');
    list.replaceChildren();
    if (!data.conversations.length) {
      const empty = document.createElement('p');
      empty.className = 'empty-state';
      empty.textContent = 'Чатов пока нет. Узнайте тег друга или поделитесь с ним своим тегом.';
      list.append(empty);
    }
    for (const chat of data.conversations) {
      const card = document.createElement('article');
      card.className = 'chat-list-card';
      const title = document.createElement('strong');
      title.textContent = `${chat.peer.name}${chat.peer.tag ? ` · @${chat.peer.tag}` : ''}`;
      const preview = document.createElement('span');
      preview.textContent = chat.preview;
      card.append(title, preview);
      if (chat.unread) {
        const badge = document.createElement('small');
        badge.className = 'chat-unread';
        badge.textContent = `${chat.unread} новых`;
        card.append(badge);
      }
      if (productId) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'button button-accent';
        button.textContent = 'Отправить сюда ↗';
        button.addEventListener('click', async () => {
          button.disabled = true;
          try { await sendProduct(chat.id); }
          catch (error) { notice(error.message, true); button.disabled = false; }
        });
        card.append(button);
      } else {
        const link = document.createElement('a');
        link.href = chat.url;
        link.className = 'text-button';
        link.textContent = 'Открыть чат ↗';
        card.append(link);
      }
      list.append(card);
    }
    return data;
  }

  function setupList() {
    let savedTag = '';
    const copyTag = document.getElementById('chat-copy-tag');
    copyTag.addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(`@${savedTag}`); notice('Тег скопирован. Отправьте его другу.'); }
      catch { notice(`Ваш тег: @${savedTag}`); }
    });
    showList().then(data => { savedTag = data.tag; copyTag.hidden = !savedTag; }).catch(error => notice(error.message, true));
    document.getElementById('chat-tag-form').addEventListener('submit', async event => {
      event.preventDefault();
      const button = event.currentTarget.querySelector('button');
      button.disabled = true;
      try {
        const data = await api('/api/chats/tag/', {tag: document.getElementById('chat-own-tag').value});
        document.getElementById('chat-own-tag').value = `@${data.tag}`;
        savedTag = data.tag;
        copyTag.hidden = false;
        notice(`Ваш тег: @${data.tag}. Теперь по нему можно найти вас в магазине.`);
      } catch (error) { notice(error.message, true); }
      finally { button.disabled = false; }
    });
    document.getElementById('chat-find-form').addEventListener('submit', async event => {
      event.preventDefault();
      const button = event.currentTarget.querySelector('button');
      button.disabled = true;
      try {
        const data = await api('/api/chats/start/', {tag: document.getElementById('chat-find-tag').value});
        if (productId) await sendProduct(data.id);
        else location.assign(data.url);
      } catch (error) { notice(error.message, true); button.disabled = false; }
    });
  }

  function messageNode(message) {
    const bubble = document.createElement('article');
    bubble.className = `chat-bubble${message.mine ? ' mine' : ''}`;
    bubble.dataset.messageId = String(message.id);
    if (message.product) {
      const product = document.createElement(message.product.url ? 'a' : 'div');
      product.className = 'chat-product';
      if (message.product.url) product.href = message.product.url;
      if (message.product.image) {
        const image = document.createElement('img');
        image.src = message.product.image;
        image.alt = '';
        product.append(image);
      }
      const title = document.createElement('strong');
      title.textContent = message.product.name;
      product.append(title);
      if (message.product.price) {
        const price = document.createElement('span');
        price.className = 'chat-product-price';
        price.textContent = `${Math.round(Number(message.product.price)).toLocaleString('ru-RU')} ₽`;
        product.append(price);
      }
      bubble.append(product);
    }
    if (message.text) {
      const text = document.createElement('p');
      text.textContent = message.text;
      bubble.append(text);
    }
    const time = document.createElement('small');
    time.textContent = message.created;
    bubble.append(time);
    return bubble;
  }

  function setupConversation() {
    const view = document.getElementById('chat-thread-view');
    const box = document.getElementById('chat-messages');
    const older = document.getElementById('chat-older');
    const blockButton = document.getElementById('chat-block');
    const blockNote = document.getElementById('chat-blocked-note');
    const form = document.getElementById('chat-compose');
    let pendingText = null;
    const known = new Set();
    let oldest = 0;
    let newest = 0;
    let blockedByMe = false;
    const applyBlock = data => {
      blockedByMe = data.blocked_by_me;
      const blocked = data.blocked_by_me || data.blocked_by_peer;
      blockButton.textContent = blockedByMe ? 'Разблокировать' : 'Заблокировать';
      blockNote.hidden = !blocked;
      blockNote.textContent = data.blocked_by_me ? 'Вы заблокировали переписку. Новые сообщения не отправляются.' : 'Собеседник закрыл отправку сообщений в этом чате.';
      form.hidden = blocked;
    };
    const append = (messages, prepend = false) => {
      const scroll = box.scrollHeight - box.scrollTop - box.clientHeight < 90;
      const height = box.scrollHeight;
      for (const message of (prepend ? [...messages].reverse() : messages)) {
        if (known.has(message.id)) continue;
        known.add(message.id);
        if (prepend) box.prepend(messageNode(message));
        else box.append(messageNode(message));
        oldest = oldest ? Math.min(oldest, message.id) : message.id;
        newest = Math.max(newest, message.id);
      }
      if (prepend) box.scrollTop += box.scrollHeight - height;
      else if (scroll) box.scrollTop = box.scrollHeight;
    };
    const endpoint = `/api/chats/${conversationId}/`;
    api(endpoint).then(data => {
      view.hidden = false;
      document.getElementById('chat-peer').textContent = data.peer.name;
      document.getElementById('chat-peer-tag').textContent = data.peer.tag ? `@${data.peer.tag}` : '';
      applyBlock(data);
      append(data.messages);
      older.hidden = !data.has_more;
    }).catch(error => notice(error.message, true));
    blockButton.addEventListener('click', async () => {
      blockButton.disabled = true;
      try {
        const data = await api(`${endpoint}block/`, {blocked: !blockedByMe});
        const current = await api(endpoint);
        applyBlock({...current, ...data});
      } catch (error) { notice(error.message, true); }
      finally { blockButton.disabled = false; }
    });
    older.addEventListener('click', async () => {
      older.disabled = true;
      try {
        const data = await api(`${endpoint}?before=${oldest}`);
        append(data.messages, true);
        older.hidden = !data.has_more;
      } catch (error) { notice(error.message, true); }
      finally { older.disabled = false; }
    });
    form.addEventListener('submit', async event => {
      event.preventDefault();
      const input = document.getElementById('chat-text');
      const button = form.querySelector('button');
      button.disabled = true;
      try {
        const content = input.value;
        if (!pendingText || pendingText.content !== content) pendingText = {content, token: nonce()};
        const data = await api(`${endpoint}send/`, {text: content, nonce: pendingText.token});
        pendingText = null;
        input.value = '';
        append([data.message]);
      } catch (error) { notice(error.message, true); }
      finally { button.disabled = false; }
    });
    setInterval(async () => {
      if (view.hidden || document.hidden) return;
      try {
        const data = await api(`${endpoint}${newest ? `?after=${newest}` : ''}`);
        applyBlock(data);
        append(data.messages);
      } catch { /* Try again on the next poll. */ }
    }, 5000);
  }

  if (conversationId) setupConversation();
  else setupList();
})();
