import datetime
from collections import defaultdict
import numpy as np
import pymongo
import time

import util.util_domain_decomposition as udd
import util.util_db_access as uda
import util.util_measurements as um
import util.pollutant as pollutant

""" 

Description:
    This script pre-processes data for the RNN model. The data is taken from the 
    pollution_estimates database, which is created using the run_caline_model script. 
    The pre-processing shapes the data into the form
    
        sub_domain = id
        run_tag    = run_tag
        input      = [timestamp, wind_dir, wind_speed, wind_dir_std, temperature, 
                      lat_link1, lon_link1, lat_link2, lon_link2, volume, ... x20, 
                      lat, lon, ... x20]
        labels     = [NO2_value, PM10_value, PM25_value, ... x20]
    
    All of the input data is normalized. 
    
    Caline is only modeling the contribution to the pollution levels that is coming from traffic. 
    Taking the background pollution levels into account, the collected output is given by
    
        output = Caline(traffic, weather, default background) - default background + real background
    
    The default background is a static value, whereas the real background depends on the time.
    In order to only model the contribution by Caline, we model
    
        labels = output - real background


-*- coding: utf-8 -*-

Legal:
    (C) Copyright IBM 2018.
    
    This code is licensed under the Apache License, Version 2.0. You may
    obtain a copy of this license in the LICENSE.txt file in the root directory
    of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
    
    Any modifications or derivative works of this code must retain this
    copyright notice, and modified files need to carry a notice indicating
    that they have been altered from the originals.

    IBM-Review-Requirement: Art30.3
    Please note that the following code was developed for the project VaVeL at
    IBM Research -- Ireland, funded by the European Union under the
    Horizon 2020 Program.
    The project started on December 1st, 2015 and was completed by December 1st,
    2018. Thus, in accordance with Article 30.3 of the Multi-Beneficiary General
    Model Grant Agreement of the Program, there are certain limitations in force 
    up to December 1st, 2022. For further details please contact Jakub Marecek
    (jakub.marecek@ie.ibm.com) or Gal Weiss (wgal@ie.ibm.com).

If you use the code, please cite our paper:
https://arxiv.org/abs/1810.09425

Author: 
    Philipp Hähnel <phahnel@hsph.harvard.edu>

Last updated:
    2019 - 05 - 09
    
"""


def get_parameters():
    """
    :return:
    """
    param = {'mesh size': 2,
             #  time slice (2017-07-01 01:00:00 to 2018-05-02 14:00:00)
             'date start': datetime.datetime(2017, 7, 1, 0),
             'date end': datetime.datetime(2018, 5, 2, 23),}
    # param['sub domain selection'] = list(range(1, 12 + 1))
    param['sub domain selection'] = [6, 7]
    # runs selectors from the caline_estimates
    tag = '2019-05-21 10 '
    distances = [5, 6, 7, 8, 9, 10, 11, 13, 17, 27, 53, 103]
    param['runs'] = [tag + str(dist) for dist in distances]
    # param['runs'] = ['2019-05-09 10 100']

    return param


def pre_process(date_start, date_end, collection_pre, mesh,
                weather_data, background_pollution, traffic_volumes, utilities,
                caline_estimates, receptor_list):
    """
        The pre-processing is split up for each individual run because of the
        potentially high memory burden.

        TODO: make pre-processed receptors easily findable

    :param date_start:
    :param date_end:
    :param collection_pre:
    :param mesh:
    :param weather_data: {timestamp: [wind_dir, wind_speed, wind_dir_std, temp]}
    :param background_pollution:
    :param traffic_volumes:
    :param utilities:
    :param caline_estimates:
    :param receptor_list:
    :return:
    """

    def normalize(val, mean, std):
        return (val - mean) / std

    def normalize_time(_timestamp):
        """ :param _timestamp: timestamp """
        return normalize(_timestamp, time_mean, time_std)

    def normalize_weather(_weather_list):
        """ :param _weather_list: [wind_dir, wind_speed, wind_dir_std, temp] """
        return list(normalize(np.asarray(_weather_list), weather_mean, weather_std))

    def normalize_traffic(_volume):
        """ :param _volume: (int) traffic volume """
        return normalize(_volume, volumes_mean, volumes_std)

    def normalize_coords(_coords):
        """ :param _coords: [lat, lon] """
        return list(normalize(np.asarray(_coords), coord_mean, coord_std))

    time_interval = [date_start.timestamp(), date_end.timestamp()]
    time_mean = np.mean(time_interval)
    time_std = np.std(time_interval)

    weather_values = np.transpose(list(weather_data.values()))
    weather_mean = np.mean(weather_values, 1)
    weather_std = np.std(weather_values, 1)

    volumes = [volume for series in traffic_volumes.values() for volume in series.values()]
    volumes_mean = np.mean(volumes)
    volumes_std = np.std(volumes)

    bounding_box = np.transpose(list(utilities['bounding_box'].values()))
    coord_mean = np.mean(bounding_box, 1)
    coord_std = np.std(bounding_box, 1)

    current_background_pollution = um.get_empirical_background_pollution()

    # max_links = max([len(box['links']) for box in mesh.values()])

    receptor_coords = defaultdict(list)
    for sub_domain_id, sub_domain in mesh.items():
        for receptor in receptor_list:
            if udd.is_point_in_area(receptor, sub_domain['coord']):
                receptor_coords[sub_domain_id].append(receptor)
        receptor_coords[sub_domain_id].sort(key=lambda x: (x[0], x[1]))

    print('Pre-processing ...')
    date_current = date_start
    time_step = datetime.timedelta(hours=1)
    # collect entries before writing them into database as database access is comparatively slow.
    collection_of_pre_processed_entries = []
    max_collection_size = 100000

    while date_current <= date_end:
        current_timestamp = date_current.timestamp()

        if current_timestamp not in weather_data:
            date_current += time_step
            continue
        current_weather_data = weather_data[current_timestamp]

        if current_timestamp not in traffic_volumes:
            date_current += time_step
            continue

        if current_timestamp in background_pollution:
            # update available background pollution
            for pol, value in background_pollution[current_timestamp].items():
                current_background_pollution[pol] = value
        # else: take previous background pollution

        for sub_domain_id, sub_domain in mesh.items():
            if current_timestamp not in caline_estimates:
                continue

            # links are ordered tuples in traffic volumes, but not in links_in_area
            links = []
            links_coords = []
            for link in sub_domain['links']:
                links.append(tuple(sorted(link)))
                links_coords.append(utilities['links_in_area'][tuple(link)])

            current_traffic_volume = []
            for i, link in enumerate(links):
                current_traffic_volume += normalize_coords(links_coords[i][0])
                current_traffic_volume += normalize_coords(links_coords[i][1])
                if link in traffic_volumes[current_timestamp]:
                    current_traffic_volume += [normalize_traffic(traffic_volumes[current_timestamp][link])]
                else:
                    current_traffic_volume += [0]
            # maximally 20 links in sub-domain; if less, pad with zeros
            current_traffic_volume += [0] * ((20 - len(links)) * 5)

            current_receptors = []
            current_estimates = []
            for i, coord in enumerate(receptor_coords[sub_domain_id]):
                current_receptors += normalize_coords(coord)
                for pollution_type in pollutant.Pollutant:
                    poll = pollution_type.get_name()
                    if coord in caline_estimates[current_timestamp] \
                            and poll in caline_estimates[current_timestamp][coord]:
                        value = caline_estimates[current_timestamp][coord][poll] \
                                - current_background_pollution[poll]
                    else:
                        value = 0
                    value = 0 if value < -1 else value  # Caline output should not be largely negative!
                    current_estimates.append(value)

            timestamp = [normalize_time(current_timestamp)]
            weather = normalize_weather(current_weather_data)

            input_data = timestamp + weather + current_traffic_volume + current_receptors
            labels = current_estimates

            data = {'mesh_size': len(mesh),
                    'sub_domain': sub_domain_id,
                    'input': input_data,
                    'labels': labels}

            collection_of_pre_processed_entries.append(data)

            if len(collection_of_pre_processed_entries) < max_collection_size:
                continue
            collection_pre.insert_many(collection_of_pre_processed_entries)
            collection_of_pre_processed_entries = []
            print(f'Complete up to {date_current:%Y-%M-%d %H}.')

        date_current += time_step

    # collect the last batch if not empty
    if collection_of_pre_processed_entries:
        collection_pre.insert_many(collection_of_pre_processed_entries)

    return None


def main():
    """
        Schematic flow:

        Get parameters.
        Connect to database.
        Get weather data.
        Get background pollution data from measurements.
        For each run in runs:
            Get utility data.
            Get traffic data.
            Get Caline estimates.
            Pre-process into the form
                input = [timestamp, wind_dir, wind_speed, wind_dir_std, temperature,
                         lat_src_start, lon_src_start, lat_src_end, lon_src_end, volume, ... x20,
                         lat_rec, lon_rec, ... x20]
                labels = [rec_NO2_value, rec_PM10_value, rec_PM25_value, ... x20]

    :return: None
    """
    param = get_parameters()

    print('Connecting to internal Mongo database ...')
    client_internal = pymongo.MongoClient('localhost', 27018)
    collection_weather = client_internal.db_air_quality.weather
    collection_traffic_volumes = client_internal.db_air_quality.traffic_volumes
    collection_caline_estimates = client_internal.db_air_quality.caline_estimates
    collection_measurement = client_internal.db_air_quality.pollution_measurements
    collection_util = client_internal.db_air_quality.util
    # collection to store the pre-processed data
    collection_pre = client_internal.db_air_quality.proc_estimates

    print('Getting weather data ...')
    weather_data = uda.get_weather_data(collection_weather, param['date start'], param['date end'])

    print('Getting background pollution data ...')
    background_pollution = uda.get_background_pollution(collection_measurement,
                                                        param['date start'], param['date end'])

    print(f'Mesh size: {param["mesh size"]}\n')

    for run_tag in param['runs']:
        print(f'Pre-processing for run {run_tag}:')

        print('Getting utility stuff ... ')
        utilities = uda.get_utilities_from_collection(collection_util, run_tag)
        if not len(utilities):
            print(f'No utility data for run {run_tag}. Continue with next run.')
            continue

        # restrict to only those sub-domains that we select for
        utilities['domain_dict'] = {key: value for key, value in utilities['domain_dict'].items()
                                    if key in param["sub domain selection"]}

        if not len(utilities['domain_dict']):
            print(f'No receptors in the selected sub-domains {param["sub domain selection"]} for run {run_tag}.')
            continue

        print('Getting traffic data ...')
        traffic_links = [link for tile in utilities['domain_dict'].values() for link in tile['links']]
        traffic_links_sorted = [sorted(link) for link in traffic_links]
        traffic_volumes = uda.get_traffic_volumes(collection_traffic_volumes,
                                                  param['date start'], param['date end'],
                                                  traffic_links_sorted)

        caline_estimates, receptor_list = uda.get_caline_estimates(collection_caline_estimates,
                                                                   param['date start'], param['date end'], run_tag)

        if param["mesh size"] == 1:
            # one-tile mesh:
            mesh = {1: {'coord': utilities['bounding_box'],
                        'links': traffic_links}}
        else:
            # many-tile mesh:
            mesh = utilities['domain_dict']

        t = time.perf_counter()
        pre_process(param['date start'], param['date end'], collection_pre, mesh,
                    weather_data, background_pollution, traffic_volumes, utilities,
                    caline_estimates, receptor_list)
        elaps = time.perf_counter() - t
        print(f'{run_tag} complete ({elaps:.2f})s.')
        print('')

    return None


if __name__ == '__main__':
    main()
