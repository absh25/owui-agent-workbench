/* Jira review form. The server validates the returned draft before any write. */
(() => {
  'use strict';

  let activeDialog = null;
  let sequence = 0;

  const element = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  const validate = (data) => {
    const form = data && data.jira_form;
    if (!form || form.version !== 1 || typeof form.request_id !== 'string' ||
        !form.request_id || form.request_id.length > 256 ||
        !Array.isArray(form.fields) || !form.fields.length || form.fields.length > 24) {
      throw new Error('Invalid Jira review request');
    }
    const names = new Set();
    for (const field of form.fields) {
      if (!field || typeof field.name !== 'string' ||
          !/^[a-z][a-z0-9_]{0,63}$/.test(field.name) || names.has(field.name) ||
          typeof field.value !== 'string' || typeof field.label !== 'string' ||
          (field.options !== undefined && (!Array.isArray(field.options) ||
            field.options.length > 256 || field.options.some(option =>
              !option || typeof option.value !== 'string' || typeof option.label !== 'string')))) {
        throw new Error('Invalid Jira review field');
      }
      names.add(field.name);
    }
    return form;
  };

  window.openJiraConfirmation = (data) => {
    let definition;
    try {
      definition = validate(data);
      if (activeDialog) throw new Error('A Jira review is already open');
    } catch (error) {
      return Promise.reject(error);
    }

    return new Promise((resolve, reject) => {
      const id = `jira-review-${++sequence}`;
      const previousFocus = document.activeElement;
      const dialog = element('dialog', 'jira-review');
      const form = element('form', 'jira-review__form');
      const header = element('header', 'jira-review__header');
      const heading = element('div', 'jira-review__heading');
      const badge = element('span', 'jira-review__badge', 'Jira');
      const title = element('h2', 'jira-review__title',
        typeof data.title === 'string' && data.title ? data.title : '确认提交内容');
      title.id = `${id}-title`;
      const subtitle = element('p', 'jira-review__subtitle', '核对并修改下方内容，确认后才会提交到 Jira。');
      subtitle.id = `${id}-description`;
      dialog.setAttribute('aria-labelledby', title.id);
      dialog.setAttribute('aria-describedby', subtitle.id);
      const close = element('button', 'jira-review__close', '×');
      close.type = 'button';
      close.setAttribute('aria-label', '关闭并取消提交');
      heading.append(badge, title, subtitle);
      header.append(heading, close);

      const content = element('div', 'jira-review__content');
      const operation = typeof definition.operation === 'string' ? definition.operation : '提交变更';
      const notice = element('div', 'jira-review__notice');
      const noticeMark = element('span', 'jira-review__notice-mark', '✓');
      noticeMark.setAttribute('aria-hidden', 'true');
      const noticeText = element('div');
      noticeText.append(element('strong', '', `待确认：${operation}`),
        element('p', '', '你可以直接编辑。取消会停止本次提交。'));
      notice.append(noticeMark, noticeText);
      const grid = element('div', 'jira-review__grid');
      const inputs = new Map();

      definition.fields.forEach((field, index) => {
        const wide = field.multiline === true || field.name === 'summary';
        const group = element('div', `jira-review__field${wide ? ' jira-review__field--wide' : ''}`);
        const inputId = `${id}-field-${index}`;
        const label = element('label', 'jira-review__label');
        label.htmlFor = inputId;
        label.append(element('span', '', field.label),
          element('span', field.required ? 'jira-review__required' : 'jira-review__optional',
            field.required ? '*' : '选填'));
        let input;
        if (field.options && field.options.length) {
          input = element('select', 'jira-review__input');
          if (!field.required) {
            const empty = element('option', '', '不设置');
            empty.value = '';
            input.append(empty);
          }
          for (const choice of field.options) {
            const option = element('option', '', choice.label);
            option.value = choice.value;
            input.append(option);
          }
          // Preserve the supplied draft visibly even if its value was not in the choices.
          // The backend performs authoritative project and issue-type validation.
          if (field.value && !Array.from(input.options).some(option => option.value === field.value)) {
            const supplied = element('option', '', field.value);
            supplied.value = field.value;
            input.append(supplied);
          }
        } else if (field.multiline === true) {
          input = element('textarea', 'jira-review__input jira-review__textarea');
          input.rows = 6;
        } else {
          input = element('input', 'jira-review__input');
          input.type = 'text';
        }
        input.id = inputId;
        input.name = field.name;
        input.required = field.required === true;
        input.value = field.value;
        input.autocomplete = 'off';
        if (Number.isInteger(field.max_length) && field.max_length > 0 && field.max_length <= 100000) {
          input.maxLength = field.max_length;
        }
        if (field.read_only === true) {
          if (input.tagName === 'SELECT') input.disabled = true;
          else input.readOnly = true;
          group.classList.add('jira-review__field--readonly');
        }
        const check = () => input.setCustomValidity(
          field.required && !input.value.trim() ? '请填写此字段。' : '');
        input.addEventListener('input', check);
        input.addEventListener('change', check);
        inputs.set(field.name, {input, check});
        group.append(label, input);
        grid.append(group);
      });
      content.append(notice, grid);

      const footer = element('footer', 'jira-review__footer');
      const footerNote = element('p', 'jira-review__footer-note', '仅在你确认后执行本次提交');
      const actions = element('div', 'jira-review__actions');
      const cancel = element('button', 'jira-review__button jira-review__button--secondary', '取消');
      cancel.type = 'button';
      const submit = element('button', 'jira-review__button jira-review__button--primary', '确认并提交');
      submit.type = 'submit';
      actions.append(cancel, submit);
      footer.append(footerNote, actions);
      form.append(header, content, footer);
      dialog.append(form);

      let settled = false;
      const finish = (value) => {
        if (settled) return;
        settled = true;
        submit.disabled = true;
        cancel.disabled = true;
        close.disabled = true;
        if (dialog.open) dialog.close();
        dialog.remove();
        activeDialog = null;
        if (previousFocus && previousFocus.isConnected && typeof previousFocus.focus === 'function') {
          previousFocus.focus();
        }
        resolve(value);
      };
      close.addEventListener('click', () => finish(false));
      cancel.addEventListener('click', () => finish(false));
      dialog.addEventListener('cancel', event => {
        event.preventDefault();
        finish(false);
      });
      // A backdrop click keeps the draft open; closing or Escape explicitly cancels it.
      dialog.addEventListener('close', () => { if (!settled) finish(false); });
      form.addEventListener('submit', event => {
        event.preventDefault();
        for (const {check} of inputs.values()) check();
        if (!form.reportValidity()) return;
        const argumentsDraft = Object.create(null);
        for (const [name, {input}] of inputs) argumentsDraft[name] = input.value;
        finish({confirmed: true, request_id: definition.request_id, arguments: argumentsDraft});
      });
      dialog.addEventListener('keydown', event => {
        if (event.key !== 'Tab') return;
        const controls = Array.from(dialog.querySelectorAll('button, input, textarea, select'))
          .filter(control => !control.disabled && control.tabIndex !== -1);
        const first = controls[0];
        const last = controls[controls.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      });

      activeDialog = dialog;
      document.body.append(dialog);
      try {
        dialog.showModal();
        const initial = inputs.get('summary') || Array.from(inputs.values()).find(item =>
          !item.input.disabled && !item.input.readOnly);
        if (initial) initial.input.focus();
        else cancel.focus();
      } catch (error) {
        dialog.remove();
        activeDialog = null;
        reject(error);
      }
    });
  };
})();
