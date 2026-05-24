import streamlit as st
import asyncio
import json
import os
import re
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

st.set_page_config(page_title="Выгрузка ЕРВК", page_icon="📋")

st.title("Выгрузка уведомлений с сайта ЕРВК")
st.markdown("Программа автоматически откроет сайт, введет нужные фильтры, пролистает все страницы и соберет данные.")

# Настройки поиска (по умолчанию подставлены ваши значения)
st.subheader("Настройки фильтров")
activity_filter = st.text_input("Вид деятельности (поиск)", "Предоставление услуг общественного питания организациями общественного питания")
region_filter = st.text_input("Субъект РФ (поиск)", "Москва")
region_exact = st.text_input("Точное название Субъекта РФ в выпадающем списке", "Г. Москва")

# Асинхронная функция парсинга
async def run_scraper(status_container, progress_container, stats_container, activity, region, exact_region):
    output_file = "notices.json"
    if os.path.exists(output_file):
        os.remove(output_file)
        
    all_notices = []
    seen_ids = set()
    
    # Переменные для статистики
    total_matches_str = "Неизвестно"
    pages_to_process = "Неизвестно"
    records_per_page = 0

    async with async_playwright() as p:
        # Запускаем браузер
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        status_container.info("⏳ Открываем сайт ervk.gov.ru...")
        await page.goto("https://ervk.gov.ru/public/notices", wait_until="networkidle")
        
        status_container.info("⏳ Открываем расширенный поиск...")
        await page.click("text=Расширенный поиск")
        await page.wait_for_timeout(1000)

        status_container.info("⏳ Заполняем фильтр 'Вид деятельности'...")
        await page.locator("label").filter(has_text="Вид деятельности").locator("..").locator("input").fill(activity)
        await page.wait_for_timeout(1500)
        await page.locator("li[role='option']").first.click()
        await page.wait_for_timeout(1000)

        status_container.info("⏳ Заполняем фильтр 'Субъект РФ'...")
        await page.locator("label").filter(has_text="Субъект РФ").locator("..").locator("input").fill(region)
        await page.wait_for_timeout(1500)
        await page.locator("li[role='option']").filter(has_text=exact_region).click()
        
        status_container.info("⏳ Ждем применения фильтров и загрузки данных...")
        await page.wait_for_timeout(3000)

        # Сбор первичной информации перед запуском цикла
        html = await page.content()
        soup = BeautifulSoup(html, 'html.parser')
        
        # Ищем блок "Найдено совпадений"
        el = soup.find(string=lambda t: t and 'Найдено совпадений' in t)
        if el and el.parent and el.parent.parent:
            match_text = el.parent.parent.text
            # Извлекаем число
            matches = re.findall(r'\d+', match_text)
            if matches:
                total_matches_str = matches[0]

        # Определяем количество записей на первой странице
        first_page_cards = soup.find_all('div', class_='fp-fp-MuiBox-root css-0')
        first_page_records = [c for c in first_page_cards if 'Работы или услуги:' in c.get_text()]
        records_per_page = len(first_page_records)
        
        if total_matches_str.isdigit() and records_per_page > 0:
            import math
            pages_to_process = math.ceil(int(total_matches_str) / records_per_page)
        
        # Выводим статистику в интерфейс
        stats_container.info(f"📊 **Анализ фильтров завершен!**\n\n"
                             f"Найдено совпадений: **{total_matches_str}**\n\n"
                             f"Записей на одной странице: **{records_per_page}**\n\n"
                             f"Примерное количество страниц для обхода: **{pages_to_process}**")

        page_num = 1
        while True:
            progress_container.warning(f"🔄 Идет сбор данных со страницы {page_num} из {pages_to_process}...")
            html = await page.content()
            soup = BeautifulSoup(html, 'html.parser')
            
            cards = soup.find_all('div', class_='fp-fp-MuiBox-root css-0')
            
            for card in cards:
                text = card.get_text(separator='|')
                if 'Работы или услуги:' in text:
                    parts = text.split('|')
                    try:
                        notice_id = parts[0].strip()
                        if notice_id in seen_ids:
                            continue
                        seen_ids.add(notice_id)
                        
                        def get_value_after(title):
                            try:
                                idx = parts.index(title)
                                return parts[idx+1].strip() if idx + 1 < len(parts) else ""
                            except ValueError:
                                return ""

                        notice_data = {
                            "номер уведомления": notice_id,
                            "наименование хозяйствующего субъекта": parts[1].strip() if len(parts) > 1 else "",
                            "дата": parts[2].replace("От ", "").strip() if len(parts) > 2 else "",
                            "наименование уведомления": parts[3].strip() if len(parts) > 3 else "",
                            "наименование работы или услуги": get_value_after("Работы или услуги:"),
                            "коды ОКВЭД": get_value_after("Коды ОКВЭД:"),
                            "Субъект РФ": get_value_after("Субъект РФ:"),
                            "Адрес места осуществления деятельности": get_value_after("Адрес места осуществления деятельности:"),
                            "Наименование контрольного органа": get_value_after("Наименование контрольного органа:")
                        }
                        all_notices.append(notice_data)
                    except Exception as e:
                        pass # Игнорируем битые карточки

            next_btn = page.locator("button[aria-label='Перейти на следующую страницу']")
            count = await next_btn.count()
            if count == 0:
                break
                
            is_disabled = await next_btn.is_disabled()
            if is_disabled:
                break
                
            await next_btn.click()
            page_num += 1
            await page.wait_for_timeout(2500)

        progress_container.empty()
        status_container.success(f"✅ Сбор завершен. Всего найдено уведомлений: {len(all_notices)}")
        
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(all_notices, f, ensure_ascii=False, indent=4)

        await browser.close()
        return output_file, all_notices

# Интерфейс
if st.button("Начать выгрузку", type="primary"):
    stats_container = st.empty()
    status_container = st.empty()
    progress_container = st.empty()
    
    with st.spinner('Браузер работает в фоновом режиме. Пожалуйста, подождите...'):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        try:
            output_file, data = loop.run_until_complete(
                run_scraper(status_container, progress_container, stats_container, activity_filter, region_filter, region_exact)
            )
        except Exception as e:
            st.error(f"Произошла ошибка при сборе: {e}")
            data = []
        finally:
            loop.close()
        
    if data:
        st.subheader("Предпросмотр данных (первые 3 записи)")
        st.json(data[:3])
        
        # Кнопка для скачивания файла
        with open(output_file, "rb") as file:
            st.download_button(
                label="📥 Скачать notices.json",
                data=file,
                file_name="notices.json",
                mime="application/json",
                type="primary"
            )
