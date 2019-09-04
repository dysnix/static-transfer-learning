from __future__ import absolute_import, division, print_function, unicode_literals

import random
import csv
import os
import logging
import shutil

import ipfsapi
import settings
import async_timeout
from aiofile import AIOFile
from asyncio_pool import AioPool
from aiohttp import ClientSession


class DataManager(object):
    PROJECT_DIR = os.path.abspath(os.path.dirname(__file__))
    DATA_DIR = os.path.join(PROJECT_DIR, settings.DATA_DIR)
    TRAIN_DIR = os.path.join(DATA_DIR, 'train')
    VALIDATE_DIR = os.path.join(DATA_DIR, 'validate')
    TMP_DIR = os.path.join(DATA_DIR, 'tmp')
    LOG_DIR = os.path.join(DATA_DIR, 'logs')
    MODELS_DIR = os.path.join(DATA_DIR, 'models')

    def __init__(self):
        self.ipfs_client = ipfsapi.connect(settings.IPFS_HOST, settings.IPFS_PORT)
        self.pool = AioPool(size=settings.DOWNLOAD_POOL_SIZE)

    def __ipfs_save(self, file_path):
        return self.ipfs_client.add(file_path)

    def __get_ipfs(self, hash):
        return self.ipfs_client.cat(hash)

    def get_model_path(self, file_name):
        return os.path.join(self.MODELS_DIR, file_name)

    def makedirs(self, dirs):
        for path in dirs:
            os.makedirs(path, exist_ok=True)

    def cleanup(self, dirs):
        for path in dirs:
            shutil.rmtree(path, ignore_errors=True)

    def download(self, hash, path):
        with open(path, 'wb+') as f:
            f.write(self.__get_ipfs(hash))
        return path

    async def fetch(self, url):
        async with ClientSession() as session:
            with async_timeout.timeout(settings.HTTP_TIMEOUT):
                async with session.get(url) as response:
                    return await response.read()

    async def download_file(self, req):
        url = req['url']
        file_path = req['file_path']

        logging.debug('Downloading {url} into {file_path}'.format(url=url, file_path=file_path))

        response = await self.fetch(url)
        try:
            async with AIOFile(file_path, 'wb+') as afp:
                await afp.write(response)
                await afp.fsync()
                logging.debug("File {url} downloaded to {file_path}".format(url=url, file_path=file_path))
        except Exception as exc:
            logging.warning("Error downloading file {url}: {exc}".format(url=url, exc=str(exc)))
            os.remove(file_path)

        if not os.stat(file_path).st_size:
            logging.warning("File {file_path} is empty".format(file_path=file_path))
            os.remove(file_path)

        return True

    async def get_links_for_train(self, csv_url):
        result = []
        result_by_labels = {}

        raw_csv = await self.fetch(csv_url)
        csv_lines = [i.decode('utf8') for i in raw_csv.splitlines()]

        random.seed(1)
        random.shuffle(csv_lines)

        reader = csv.reader(csv_lines, delimiter=',', quotechar='|')
        for url, label in reader:
            if label not in result_by_labels:
                result_by_labels[label] = []

            result_by_labels[label].append(url)

        labels_count = len(result_by_labels.keys())

        logging.info("Found {labels_count} labels in CSV".format(labels_count=labels_count))

        label_imgs_limit = min([len(result_by_labels[i]) for i in result_by_labels])

        if settings.DATA_LIMIT and label_imgs_limit > settings.DATA_LIMIT:
            label_imgs_limit = settings.DATA_LIMIT

        train_size = round(label_imgs_limit * settings.TRAIN_PERCENTAGE)
        validate_size = label_imgs_limit - train_size

        logging.info(
            'Found {label_imgs_limit} lines in csv. Train size: {train_size} / Validate size: {validate_size}'.format(
                label_imgs_limit=label_imgs_limit,
                train_size=train_size,
                validate_size=validate_size
            )
        )

        for counter in range(0, label_imgs_limit):
            if counter <= train_size:
                i_type = 'train'
            else:
                i_type = 'validate'

            for label in result_by_labels.keys():
                url = result_by_labels[label].pop()
                result.append({
                    'url': url,
                    'label': label,
                    'i_type': i_type,
                    'file_name': '{counter}.jpg'.format(counter=counter)
                })

        return result, train_size, validate_size

    async def download_train_data(self, csv_url):
        tasks = []
        created_label_dirs = []

        self.makedirs([self.DATA_DIR, self.TRAIN_DIR, self.VALIDATE_DIR])

        links, train_size, validate_size = await self.get_links_for_train(csv_url)
        logging.info('Found {count} links'.format(count=len(links)))

        for link in links:
            dir_path = os.path.join(self.DATA_DIR, link['i_type'], link['label'])
            file_path = os.path.join(dir_path, link['file_name'])

            if dir_path not in created_label_dirs:
                os.makedirs(dir_path, exist_ok=True)
                created_label_dirs.append(dir_path)

            if not os.path.isfile(file_path):
                tasks.append({
                    "url": link['url'],
                    "file_path": file_path
                })

        if tasks:
            await self.pool.map(self.download_file, tasks)

        logging.info('Data downloaded ({count} files)'.format(count=len(tasks)))

        return train_size, validate_size