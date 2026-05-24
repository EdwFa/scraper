import streamlit as st
import asyncio
import json
import os
import re
import logging
import datetime
import urllib.parse
import aiohttp
from playwright.async_api import async_playwright

# Настройка логирования
logging.basicConfig(
    filename='scraper.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

st.set_page_config(page_title="Выгрузка ЕРВК", page_icon="📋", layout="wide")

st.title("Выгрузка уведомлений с сайта ЕРВК (API Версия)")
st.markdown("Программа перехватывает защитный токен и скачивает расширенные данные (вкл. ИНН и данные о контролируемых лицах) напрямую с серверов со скоростью до 1000 записей за секунды.")

# Настройки поиска
st.subheader("Настройки фильтров")
activity_filter = st.text_input("Вид деятельности (поиск)", "Предоставление услуг общественного питания организациями общественного питания")
region_filter = st.text_input("Субъект РФ (поиск)", "Москва")
region_exact = st.text_input("Точное название Субъекта РФ в выпадающем списке", "Г. Москва")

async def run_scraper(status_container, progress_container, stats_container, activity, region, exact_region):
    logging.info(f"=== ЗАПУСК ПАРСЕРА ===")
    logging.info(f"Фильтры - Вид деятельности: '{activity}', Субъект РФ: '{exact_region}'")
    
    output_file = "notices.json"
    if os.path.exists(output_file):
        os.remove(output_file)
        
    auth_token = None
    search_url_template = None
    initial_data = None
    
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()

            def handle_request(req):
                nonlocal auth_token
                if "portal/public/widgets/notices" in req.url:
                    if "token" in req.headers:
                        auth_token = req.headers["token"]

            page.on("request", handle_request)
            await page.route("**/*", lambda route: route.abort() if route.request.resource_type in ["image", "media", "font", "stylesheet"] else route.continue_())

            status_container.info("⏳ Открываем сайт ervk.gov.ru и получаем ключи доступа...")
            await page.goto("https://ervk.gov.ru/public/notices", wait_until="networkidle")
            
            await page.click("text=Расширенный поиск")
            await page.wait_for_timeout(1000)

            status_container.info("⏳ Заполняем фильтры...")
            await page.locator("label").filter(has_text="Вид деятельности").locator("..").locator("input").fill(activity)
            await page.wait_for_timeout(1500)
            await page.locator("li[role='option']").first.click()
            await page.wait_for_timeout(1000)

            await page.locator("label").filter(has_text="Субъект РФ").locator("..").locator("input").fill(region)
            await page.wait_for_timeout(1500)
            
            async with page.expect_response(lambda r: "portal/public/widgets/notices" in r.url and "regionCodeList" in r.url, timeout=60000) as response_info:
                await page.locator("li[role='option']").filter(has_text=exact_region).click()
            
            final_response = await response_info.value
            initial_data = await final_response.json()
            search_url_template = final_response.url
            
            await browser.close()
            
        if not auth_token:
            raise Exception("Не удалось перехватить токен авторизации!")
            
        total_elements = initial_data.get("totalElements", 0)
        pages_to_process = (total_elements + 999) // 1000

        logging.info(f"Анализ API завершен. Найдено совпадений: {total_elements}, страниц API (по 1000 шт): {pages_to_process}")

        stats_container.info(f"📊 **Анализ фильтров завершен!**\n\n"
                             f"Найдено совпадений: **{total_elements}**\n\n"
                             f"Авторизация пройдена успешно (Токен перехвачен). Начинаем скоростное скачивание...")

        all_notices = []
        seen_ids = set()
        
        sem = asyncio.Semaphore(10)  # Не больше 10 одновременных запросов к деталям

        async def fetch_details(session, notice_id):
            url = f"https://ervk.gov.ru/portal/public/notices/{notice_id}"
            headers = {"token": auth_token, "accept": "application/json"}
            async with sem:
                try:
                    async with session.get(url, headers=headers) as resp:
                        if resp.status == 200:
                            return await resp.json()
                except Exception as e:
                    logging.error(f"Ошибка получения деталей {notice_id}: {e}")
            return None

        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            for page_idx in range(pages_to_process):
                progress_container.warning(f"🔄 Скачиваем страницу {page_idx + 1} из {pages_to_process} (записи с {page_idx * 1000} по {(page_idx + 1) * 1000})...")
                
                # Формируем URL для списка из 1000 ID
                page_url = re.sub(r'size=\d+', 'size=1000', search_url_template)
                page_url = re.sub(r'page=\d+', f'page={page_idx}', page_url)
                
                async with session.get(page_url, headers={"token": auth_token}) as resp:
                    if resp.status != 200:
                        logging.error(f"Ошибка загрузки страницы {page_idx}: {resp.status}")
                        continue
                    page_data = await resp.json()
                    
                notices_list = page_data.get("notices", [])
                notice_ids = [n["id"] for n in notices_list if n["id"] not in seen_ids]
                
                # Параллельное скачивание деталей для всей 1000 записей
                progress_container.info(f"⚡ Скачиваем детали (ИНН, ОГРН и др.) для {len(notice_ids)} записей (может занять 20-30 секунд)...")
                tasks = [fetch_details(session, nid) for nid in notice_ids]
                details_results = await asyncio.gather(*tasks)
                
                for detail in details_results:
                    if not detail:
                        continue
                        
                    notice_id_str = str(detail.get("id"))
                    seen_ids.add(notice_id_str)
                    
                    # Извлечение ИНН и субъекта из подструктур
                    legal_entity = detail.get("legalEntity")
                    ip_entity = detail.get("individualEntrepreneur")
                    
                    person_data = {}
                    global_inn = ""
                    
                    person_type = ""
                    
                    if legal_entity:
                        global_inn = legal_entity.get("inn", "")
                        person_type = "Юридические лица"
                        person_data = {
                            "Полное наименование": legal_entity.get("fullName", ""),
                            "Краткое наименование": legal_entity.get("shortName", ""),
                            "Адрес": legal_entity.get("address", ""),
                            "ИНН": global_inn,
                            "ОГРН": legal_entity.get("ogrn", "")
                        }
                    elif ip_entity:
                        global_inn = ip_entity.get("inn", "")
                        person_type = "Индивидуальные предприниматели"
                        person_data = {
                            "ФИО": ip_entity.get("fio", ""),
                            "ИНН": global_inn,
                            "ОГРНИП": ip_entity.get("ogrnip", "")
                        }
                        
                    control_obj = detail.get("controlObject", {})
                    control_org = detail.get("controlOrgan", {})
                    activity_obj = detail.get("activity", {})

                    notice_data = {
                        "номер уведомления": detail.get("number", ""),
                        "наименование хозяйствующего субъекта": control_obj.get("name", ""),
                        "дата": detail.get("noticeDate", ""),
                        "наименование работы или услуги": activity_obj.get("workAndServiceTitle", ""),
                        "коды ОКВЭД": detail.get("okvedList", ""),
                        "Субъект РФ": control_obj.get("regionTitle", ""),
                        "Адрес места осуществления деятельности": control_obj.get("address", ""),
                        "Наименование контрольного органа": control_org.get("title", ""),
                        "ИНН": global_inn,
                        "Тип": person_type,
                        "Контролируемое лицо": person_data
                    }
                    all_notices.append(notice_data)
                    
                logging.info(f"Страница {page_idx + 1} обработана. Всего собрано на данный момент: {len(all_notices)}")

        progress_container.empty()
        status_container.success(f"✅ Сбор завершен. Всего найдено и сохранено уведомлений: {len(all_notices)}")
        
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(all_notices, f, ensure_ascii=False, indent=4)
            
        logging.info(f"Сбор успешно завершен. Ожидалось записей: {total_elements}, фактически собрано и записано в JSON: {len(all_notices)}")

        return output_file, all_notices
            
    except Exception as ex:
        logging.error(f"Критическая ошибка в процессе работы парсера: {ex}", exc_info=True)
        raise ex

# Интерфейс
if st.button("Начать выгрузку", type="primary"):
    stats_container = st.empty()
    status_container = st.empty()
    progress_container = st.empty()
    
    with st.spinner('Скрипт перехватывает доступ к API...'):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        try:
            output_file, data = loop.run_until_complete(
                run_scraper(status_container, progress_container, stats_container, activity_filter, region_filter, region_exact)
            )
        except Exception as e:
            st.error(f"Произошла ошибка при сборе: см. файл логов scraper.log")
            logging.info(f"=== ЗАВЕРШЕНО С ОШИБКОЙ ===\n{e}")
            data = []
        finally:
            loop.close()
        
    if data:
        st.subheader("Предпросмотр данных (первые 3 записи)")
        st.json(data[:3])
        
        with open(output_file, "rb") as file:
            st.download_button(
                label="📥 Скачать notices.json",
                data=file,
                file_name="notices.json",
                mime="application/json",
                type="primary"
            )
        logging.info(f"=== ЗАВЕРШЕНО УСПЕШНО ===\n")
