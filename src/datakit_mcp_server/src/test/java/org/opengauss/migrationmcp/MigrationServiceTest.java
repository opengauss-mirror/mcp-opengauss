package org.opengauss.migrationmcp;

import org.junit.jupiter.api.Test;
import org.opengauss.migrationmcp.service.MigrationService;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;

@SpringBootTest
public class MigrationServiceTest {

    @Autowired
    MigrationService migrationService;

    @Test
    void createTaskTest() {
        String sourceDatabaseIp = "192.168.0.118";
        String sourceDatabasePort = "3306";
        String sourceDatabaseName = "source_db";
        String targetDatabaseIp = "192.168.2.59";
        String targetDatabasePort = "5432";
        String targetDatabaseName = "target_db";
        String taskName = "testTask";

        String string = migrationService.createMigrationTask(
                sourceDatabaseIp, sourceDatabasePort, sourceDatabaseName,
                targetDatabaseIp, targetDatabasePort, targetDatabaseName, taskName);

        System.out.println(string);
    }

    @Test
    void listTaskTest() {
        migrationService.getMigrationTaskList().forEach(System.out::println);
    }

    @Test
    void startTaskTest() {
        String taskName = "testTask";
        migrationService.startMigrationTask(taskName);
    }
}
