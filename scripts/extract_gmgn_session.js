// PASTE INI DI CONSOLE BROWSER SETELAH LOGIN GMGN.AI
// Output: semua data yang dibutuhin buat WebSocket connection

(() => {
  const cookies = document.cookie.split('; ').reduce((acc, c) => {
    const [k, v] = c.split('=');
    acc[k] = v;
    return acc;
  }, {});
  
  const out = {
    cookies: {
      sid: cookies.sid || 'NOT_FOUND',
      __cf_bm: cookies.__cf_bm || 'NOT_FOUND',
      _ga: cookies._ga || 'NOT_FOUND'
    },
    device_id: localStorage.getItem('device_id') || 'GENERATE_NEW',
    fp_did: localStorage.getItem('fp_did') || localStorage.getItem('fingerprint') || 'GENERATE_NEW',
    user_uuid: localStorage.getItem('user_uuid') || localStorage.getItem('uuid') || 'GENERATE_NEW',
    access_token: '(GENERATE DI SETTINGS > API TOKEN)',
    raw_cookies: document.cookie
  };
  
  console.clear();
  console.log(JSON.stringify(out, null, 2));
  copy(JSON.stringify(out, null, 2));
  console.log('%c✅ COPIED TO CLIPBOARD! Paste ke chat', 'font-size: 16px; color: green');
})();
