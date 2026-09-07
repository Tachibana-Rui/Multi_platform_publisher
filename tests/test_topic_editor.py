"""Real Chromium regressions for rich-editor selection and topic insertion."""
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright


TAGS = ["cosplay", "莫娜cos", "莫娜女仆装", "原神", "二次元女仆cos"]


@pytest.fixture(scope="module")
def page():
    from app.publishers.browser import get_browser_executable
    with sync_playwright() as playwright:
        executable = get_browser_executable()
        if executable is None and not Path(playwright.chromium.executable_path).is_file():
            pytest.skip("Install Chromium or Edge to run rich-editor browser regressions")
        browser = playwright.chromium.launch(headless=True, **({"executable_path": str(executable)} if executable else {}))
        yield browser.new_page()
        browser.close()


@pytest.fixture
def publisher():
    from app.publishers.browser import DouyinPublisher
    publisher = DouyinPublisher()
    publisher._check_runtime_pause = lambda *_args: None
    publisher._record_human_action = lambda *_args: None
    publisher._human_pause = lambda page, *_args: page.wait_for_timeout(10)
    return publisher


def test_all_five_topics_survive_cursor_returning_inside_cosplay(page, publisher):
    page.set_content('''
        <div id="editor" contenteditable="true" style="width:600px;min-height:80px"></div>
        <div id="menu" role="listbox"></div>
        <script>
        const editor = document.querySelector('#editor');
        const menu = document.querySelector('#menu');
        window.insertions = [];
        window.bound = [];
        let modelAtEnd = false;
        function wrongCaret() {
            const node = editor.querySelector('.token')?.firstChild;
            if (!node) return;
            const range = document.createRange();
            range.setStart(node, 4); // #cos|play, as in the reported failure
            range.collapse(true);
            const selection = getSelection();
            selection.removeAllRanges(); selection.addRange(range);
        }
        editor.addEventListener('keydown', event => {
            if (event.ctrlKey && event.key === 'End') modelAtEnd = true;
        });
        editor.addEventListener('beforeinput', () => {
            // A controlled editor restores its own selection unless navigation
            // has updated that model as well as the DOM Range.
            if (!modelAtEnd) wrongCaret();
        });
        editor.addEventListener('input', event => {
            window.insertions.push(event.data);
            if (event.data === ' ') {
                modelAtEnd = false;
                wrongCaret();
                return;
            }
            const match = event.data?.match(/#([^ ]+)$/);
            if (!match) return;
            const tag = match[1];
            menu.replaceChildren();
            for (const name of tag === 'cosplay' ? ['cos', tag] : [tag]) {
                const option = document.createElement('button');
                option.setAttribute('role', 'option');
                option.textContent = '#' + name;
                option.onclick = () => {
                    const walker = document.createTreeWalker(editor, NodeFilter.SHOW_TEXT);
                    let node;
                    while (node = walker.nextNode()) {
                        const offset = node.textContent.lastIndexOf('#' + tag);
                        if (offset < 0 || node.parentElement.closest('.token')) continue;
                        const range = document.createRange();
                        range.setStart(node, offset); range.setEnd(node, offset + tag.length + 1);
                        range.deleteContents();
                        const token = document.createElement('span');
                        token.className = 'token'; token.textContent = '#' + name;
                        token.contentEditable = 'false';
                        range.insertNode(token);
                        token.after(document.createTextNode('\\u200b'));
                        window.bound.push(name);
                        break;
                    }
                    menu.replaceChildren();
                    editor.focus();
                    modelAtEnd = false;
                    wrongCaret();
                    // Framework rendering can restore the stale caret *after*
                    // the automation's first synchronous Range check succeeds.
                    setTimeout(wrongCaret, 35);
                };
                menu.append(option);
            }
        });
        </script>
    ''')
    publisher._append_and_bind_hashtags(page, page.locator('#editor'), TAGS, False)
    assert page.locator('#editor').inner_text().replace('\u200b', '') == ' '.join(f'#{tag}' for tag in TAGS)
    assert page.evaluate('window.bound') == TAGS
    assert page.evaluate('window.insertions') == ['#cosplay'] + [f' #{tag}' for tag in TAGS[1:]]
    assert publisher._bound_hashtags == TAGS
    assert not publisher._unresolved_hashtags


def test_exact_candidate_search_does_not_click_the_already_inserted_tag(page, publisher):
    page.set_content('<div contenteditable="true"><span>#cosplay</span></div><div role="listbox"><button role="option">#cosplay</button></div>')
    matches = publisher._visible_exact_topic_text_candidates(page, 'cosplay')
    assert len(matches) == 1
    assert matches[0][0].get_attribute('role') == 'option'


@pytest.mark.parametrize('markup', [
    '<textarea id="editor">#cosplay</textarea>',
    '<div id="editor" contenteditable="true"><span contenteditable="false">#cosplay</span>\u200b </div>',
])
def test_cursor_handles_textarea_and_noneditable_topic_tokens(page, publisher, markup):
    page.set_content(markup)
    editor = page.locator('#editor')
    assert publisher._place_topic_cursor_at_end(page, editor, None)
    publisher._insert_topic_text(page, editor, ' #莫娜cos')
    text = editor.evaluate("element => element.value ?? element.textContent")
    assert text.replace('\u200b', '').split() == ['#cosplay', '#莫娜cos']


def test_stubborn_editor_stops_before_inserting_into_an_old_tag(page, publisher):
    page.set_content('''<div id="editor" contenteditable="true">#cosplay</div>
        <script>
        document.querySelector('#editor').addEventListener('keydown', event => {
            if (event.key !== 'End') return;
            setTimeout(() => {
                const range = document.createRange();
                range.setStart(document.querySelector('#editor').firstChild, 4);
                range.collapse(true);
                const selection = getSelection();
                selection.removeAllRanges(); selection.addRange(range);
            }, 10);
        });
        </script>''')
    publisher._append_and_bind_hashtags(page, page.locator('#editor'), TAGS[1:], False)
    assert page.locator('#editor').inner_text() == '#cosplay'
    assert publisher._unresolved_hashtags == TAGS[1:]
