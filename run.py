import os
import sys
import json
import time
import boto3
import requests
from google import genai
from datetime import datetime
from bs4 import BeautifulSoup

gemini_client = genai.Client(api_key="your_api_key")
SENDER = "sender@mail.com"
RECIPIENT = "receipent@mail.com"

def scrape_arxiv(url, bucket_name):
    """
    Scrapes arXiv links and titles from an arXiv page, downloads the PDFs, and saves them to the specified S3 bucket.

    Args:
        url (str): The URL of the arXiv page.
        s3_bucket_name (str): The name of the S3 bucket where the PDFs and metadata will be uploaded.

    Returns:
        metadata_file_path: Path to the metadata file on the s3 bucket.

    Metadata File Structure:
        {
            "arxiv_identifier": {
                "title": "Title of the paper",
                "local_pdf_path": "Path to the downloaded PDF",
                "s3_pdf_path": "S3 path of the uploaded PDF",
                "arxiv_link": "Link to the arXiv abstract"
            }
        }
    """
    now = datetime.now()
    date_time_str = now.strftime("%Y%m%d_%H%M%S")
    local_pdfs_dir = os.path.join('data', 'raw', f'pdfs_{date_time_str}')
    metadata_file = os.path.join('data', 'raw', f'metadata_{date_time_str}.json')

    if not os.path.exists(local_pdfs_dir):
        os.makedirs(local_pdfs_dir)

    try:
        response = requests.get(url)
        response.raise_for_status()
        html_content = response.text
    except requests.exceptions.RequestException as e:
        print(f"Error fetching URL: {e}")
        return []

    soup = BeautifulSoup(html_content, 'html.parser')
    articles = soup.find_all('dl', id='articles')

    metadata = {}
    count = 0
    for article in articles:
        for dt in article.find_all('dt'):
            if count >= 10:
                break
            link_tag = dt.find('a', title='Abstract')
            if link_tag:
                arxiv_link = "https://arxiv.org" + link_tag['href']
                pdf_tag = dt.find('a', title='Download PDF')
                if pdf_tag:
                    pdf_link = "https://arxiv.org" + pdf_tag['href']
                    try:
                        response = requests.get(pdf_link)
                        response.raise_for_status()
                        pdf_filename = os.path.join(local_pdfs_dir, pdf_link.split('/')[-1] + '.pdf')
                        with open(pdf_filename, 'wb') as pdf_file:
                            pdf_file.write(response.content)
                        print(f"Downloaded PDF: {pdf_filename}")
                    except requests.exceptions.RequestException as e:
                        print(f"Error downloading PDF: {e}")
                        pdf_link = None
                else:
                    pdf_link = None

                dd = dt.find_next_sibling('dd')
                if dd:
                    title_tag = dd.find('div', class_='list-title mathjax')
                    if title_tag:
                        title = title_tag.text.replace('Title:', '').strip()
                    else:
                        title = "Title not found"
                else:
                    title = "Description not found"

                if title:
                    metadata[pdf_link.split('/')[-1]] = {'title': title, 'local_pdf_path': pdf_filename if pdf_link else None, 'arxiv_link': arxiv_link}
                    count += 1
        if count >= 10:
            break

    s3 = boto3.client('s3')

    for filename in os.listdir(local_pdfs_dir):
        if filename.endswith(".pdf"):
            s3_file_path = os.path.join(f'raw/pdfs_{date_time_str}', filename)
            local_file_path = os.path.join(local_pdfs_dir, filename)
            try:
                s3.upload_file(local_file_path, bucket_name, s3_file_path)
                print(f"Uploaded {filename} to s3://{bucket_name}/{s3_file_path}")
            except Exception as e:
                print(f"Error uploading {filename} to S3: {e}")
            
            if filename[:-4] in metadata:
                metadata[filename[:-4]]['s3_pdf_path'] = s3_file_path

    with open(metadata_file, 'w', encoding='utf-8') as json_file:
        json.dump(metadata, json_file, indent=4)
    print(f"Metadata saved to: {metadata_file}")

    s3_metadata_file_path = os.path.join('raw', f'metadata_{date_time_str}.json')
    try:
        s3.upload_file(metadata_file, bucket_name, s3_metadata_file_path)
        print(f"Uploaded metadata to s3://{bucket_name}/{s3_metadata_file_path}")
    except Exception as e:
        print(f"Error uploading metadata to S3: {e}")

    return s3_metadata_file_path

def extract_text_from_pdfs(s3_metadata_file_path, bucket_name):
    """
    Extracts text from PDFs stored in an S3 bucket using AWS Textract.

    Args:
        s3_metadata_file_path (str): The S3 path to the metadata file containing PDF information.
        bucket_name (str): The name of the S3 bucket.

    Returns:
        None
    """
    textract_client = boto3.client('textract')
    s3_client = boto3.client('s3')

    metadata_file = s3_client.get_object(Bucket=bucket_name, Key=s3_metadata_file_path)
    metadata = json.loads(metadata_file['Body'].read().decode('utf-8'))
    
    for paper_key in metadata:

        s3_pdf_path = metadata[paper_key].get('s3_pdf_path')
        try:
            response = textract_client.start_document_text_detection(
                DocumentLocation={
                    'S3Object': {
                        'Bucket': bucket_name,
                        'Name': s3_pdf_path
                    }
                }
            )
            job_id = response['JobId']
            print(f"Started text detection job for {paper_key}, Job ID: {job_id}")

            while True:
                response = textract_client.get_document_text_detection(JobId=job_id)
                status = response['JobStatus']
                if status in ['SUCCEEDED', 'FAILED']:
                    break
                time.sleep(5)
            
            if status == 'FAILED':
                raise Exception("Document analysis failed")
            
            all_blocks = response['Blocks']
            next_token = response.get('NextToken', None)

            while next_token:
                response = textract_client.get_document_text_detection(JobId=job_id, NextToken=next_token)
                all_blocks.extend(response['Blocks'])
                next_token = response.get('NextToken', None)
            
            response['Blocks'] = all_blocks
            
            text = ""
            for block in response['Blocks']:
                if block['BlockType'] == 'LINE':
                    text += block['Text'] + '\n'
            
            local_output_file = metadata[paper_key]['local_pdf_path'][:-4] + '_extracted_text.txt'
            with open(local_output_file, 'w', encoding='utf-8') as f:
                f.write(text)
            
            print(f"Text extraction completed for {paper_key}, results saved to {local_output_file}")

            s3_text_file_path = metadata[paper_key]['s3_pdf_path'][:-4] + '_text.txt'
            try:
                s3_client.upload_file(local_output_file, bucket_name, s3_text_file_path)
                print(f"Uploaded {local_output_file}_text.txt to s3://{bucket_name}/{s3_text_file_path}")
            except Exception as e:
                print(f"Error uploading {local_output_file}_text.txt to S3: {e}")

            metadata[paper_key]['local_text_path'] = local_output_file
            metadata[paper_key]['s3_text_path'] = s3_text_file_path

        except Exception as e:
            print(f"Error starting text detection for {paper_key}: {e}")

    with open('temp_metadata.json', 'w', encoding='utf-8') as meta_out:
        json.dump(metadata, meta_out, indent=4)
    s3_client.upload_file('temp_metadata.json', bucket_name, s3_metadata_file_path)
    os.remove('temp_metadata.json')

    return

def gemini_summary(text):
    """
    Generates a summary of the given text using the Gemini language model.

    Args:
        text (str): The text to summarize.

    Returns:
        str: The summary generated by the Gemini model.
    """
    evaluation_prompt = f"""**Instruction**: You are an expert summarizer. Summarize the following research paper in the domain of Computation and Language: {text}"""

    response = gemini_client.models.generate_content(
        model="gemini-2.0-flash",
        contents=evaluation_prompt
    )
    response_text = response.text

    return response_text

def summarise_and_mail(s3_metadata_file_path, bucket_name):
    """
    Summarizes the extracted text from PDFs and sends an email with the summaries.

    Args:
        s3_metadata_file_path (str): The S3 path to the metadata file containing PDF information.
        bucket_name (str): The name of the S3 bucket.

    Returns:
        None
    """
    s3_client = boto3.client('s3')

    metadata_file = s3_client.get_object(Bucket=bucket_name, Key=s3_metadata_file_path)
    metadata = json.loads(metadata_file['Body'].read().decode('utf-8'))

    for paper_key in metadata:
        s3_text_path = metadata[paper_key].get('s3_text_path')
        local_text_file = "temp_text.txt"
        s3_client.download_file(bucket_name, s3_text_path, local_text_file)
        with open(local_text_file, 'r', encoding='utf-8') as f:
            text = f.read()
        
        summary = gemini_summary(text)
        os.remove(local_text_file)

        local_summary_file = metadata[paper_key]['local_pdf_path'][:-4] + '_summary.txt'
        s3_summary_file_path = metadata[paper_key]['s3_pdf_path'][:-4] + '_summary.txt'
        with open(local_summary_file, 'w', encoding='utf-8') as f:
            f.write(summary)
        
        s3_client.upload_file(local_summary_file, bucket_name, s3_summary_file_path)
        print(f"Uploaded {local_summary_file}_summary.txt to s3://{bucket_name}/{s3_summary_file_path}")

        metadata[paper_key]['local_summary_path'] = local_summary_file
        metadata[paper_key]['s3_summary_path'] = s3_summary_file_path

    with open('temp_metadata.json', 'w', encoding='utf-8') as meta_out:
        json.dump(metadata, meta_out, indent=4)
    s3_client.upload_file('temp_metadata.json', bucket_name, s3_metadata_file_path)
    os.remove('temp_metadata.json')

    email_body = ""
    for paper_key in metadata:
        title = metadata[paper_key].get('title', 'Title not found')
        s3_summary_path = metadata[paper_key].get('s3_summary_path')
        arxiv_link = metadata[paper_key].get('arxiv_link', 'Link not found')

        local_summary_file = "temp_summary.txt"
        try:
            s3_client.download_file(bucket_name, s3_summary_path, local_summary_file)
            with open(local_summary_file, 'r', encoding='utf-8') as f:
                summary = f.read()
            os.remove(local_summary_file)
        except Exception as e:
            summary = f"Error downloading summary: {e}"

        email_body += f"Title: {title}\n"
        email_body += f"Summary: {summary}\n"
        email_body += f"Arxiv Link: {arxiv_link}\n\n"
    
    ses_client = boto3.client('ses')

    subject = "Automated Summaries of Today's ArXiV Papers."
    body_text = email_body

    ses_client.send_email(
        Destination={
            'ToAddresses': [
                RECIPIENT,
            ],
        },
        Message={
            'Body': {
                'Text': {
                    'Charset': "UTF-8",
                    'Data': body_text,
                },
            },
            'Subject': {
                'Charset': "UTF-8",
                'Data': subject,
            },
        },
        Source=SENDER,
    )

    return

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Please provide the ArXiv URL, and S3 bucket name.")
        sys.exit(1)
    url = sys.argv[1]
    bucket_name = sys.argv[2]
    metadata_path = scrape_arxiv(url, bucket_name)
    extract_text_from_pdfs(metadata_path, bucket_name)
    summarise_and_mail(metadata_path, bucket_name)
